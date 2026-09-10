# Phase 2: read-only real data

**Date:** 2026-09-10
**Status:** Approved, not yet implemented. **Amended 2026-09-10** after probing the real paper account. Three claims taken from Alpaca's published specs turned out to be wrong on this plan, and two of them leave the Markets page blocked on a decision. See *What the probe reached, and what it could not* and *Open questions*.
**Scope:** The backend (`corollary/`), and the Dashboard, Activity, Markets and Account pages
**Phase:** 2 — Alpaca paper connected, read-only. **No order reaches a broker.**

---

## Problem

PRD §11 defines Phase 2 as *"read-only, real data. Alpaca paper connected. Dashboard, Activity, Markets, Account show live values. No execution. Done when: you'd open it in the morning and learn something true."*

Two things stand between here and there.

**There is no backend.** `corollary/` is 124 lines of docstrings. `RiskManager` is nine lines of comment, `MarketDataProvider` does not exist, `db/` is a file header. `alpaca-py` is not a dependency, `httpx` is dev-only, no `alembic.ini` exists, and `vite.config.ts` has no proxy — the browser on `:5173` has no route to an API.

**Alpaca's shapes do not match the fixture shapes.** Phase 1's fixtures were written to match "the eventual API shape so Phase 2 is a data-source swap, not a component rewrite." That holds for most of the surface and fails in six specific places, each documented under Constraints below. Those six are the real work of this phase.

The sixth was found by probing the account rather than by reading the specs, and it is the worst of them: **three columns and two named screens on the Markets page have no data source on the Basic plan at all.** Not a shape mismatch that a mapper resolves — an absence. See *Open questions*.

PRD §12's open question — *"engine and API: one process or two… needs deciding before Phase 2"* — is resolved here.

---

## Constraints verified against Alpaca

The original set was read from the published API specs, not assumed. On 2026-09-10 the same claims were checked against the **real paper account** using the `.env` credentials, which work — key ID prefix `PK`, every trading endpoint 200. Several of them did not survive contact.

Everything here is verified unless marked otherwise, and each subsection now says *which kind* of verified: **spec** (read from Alpaca's OpenAPI documents or docs pages) or **probed** (observed on the live paper account today). Probed beats spec wherever the two disagree, and this amendment exists because they disagreed three times. A wrong assumption in this section reshapes the position model rather than producing a fixable bug.

### What the probe reached, and what it could not

**The paper account is completely empty.** Zero positions, zero orders, zero `FILL` activities. Its entire history is one `JNLC` funding journal. Everything downstream of a fill — the ledger, the grouper, the P&L arithmetic — was therefore checked against published shapes and Alpaca's own doc examples, never against this account's data. Decisions 4 and 5 are the ones this bites; see *Open questions* and *Testing*.

**The Alpaca MCP server is configured with non-paper keys.** It returns 401 on every trading endpoint while market data works fine. This blocks nothing — rule 3 keeps MCP out of the engine and the `.env` keys are the ones that matter — but it means **MCP is not a verification path for account, positions, orders or activities**, and nobody should spend an afternoon rediscovering that. It stays useful for docs and market data, which is how the option-event shapes below were checked.

**`ALPACA_STOCK_FEED_REALTIME` is unset in `.env`.** The other two are set (`indicative`, `sip`). CLAUDE.md requires all three and `.env.example` carries the name, so this is a setup gap rather than a design one: the value is `iex` on Basic. The design consequence is that `AlpacaProvider` must **fail loudly on a missing feed variable rather than defaulting**. A silent default is fine for the realtime var and catastrophic for the historical one — defaulting historical bars to IEX is the `min_avg_volume` error CLAUDE.md spends a paragraph on, measuring a 5,000,000 threshold against a fortieth of real volume.

### Positions are per contract, with no grouping

**Spec.** Not probed — this account holds nothing. `GET /v2/positions` keys on the OCC symbol and returns `asset_class`, `asset_id`, `symbol`, `qty`, `qty_available`, `side`, `avg_entry_price`, `cost_basis`, `market_value`, `current_price`, `lastday_price`, `change_today`, `unrealized_pl`, `unrealized_plpc`, `unrealized_intraday_pl`, `unrealized_intraday_plpc`.

It carries **no leg grouping, no order linkage, and no open date**. A four-leg iron condor is four positions. Every numeric field is a **string**, which is ideal: they parse straight to `Decimal` and no float ever touches money.

Consequence: `Position.legs[]`, the payoff curve, max-loss and the DTE column all assume one logical position. A short leg rendered alone reports as an *undefined-risk* naked short, so a defined-risk credit spread would state the wrong risk class — the exact failure CLAUDE.md rule 4 exists to prevent.

### Multi-leg orders exist; multi-leg positions do not

**Spec.** Not probed — this account has never placed an order. `order_class: "mleg"` with a `legs[]` array of `{symbol, ratio_qty, side, position_intent}`. Two rules constrain reconstruction:

- Leg ratios must be in simplest form — the GCD across `ratio_qty` values must be 1.
- Every leg must be covered within the same order. Alpaca rejects an mleg order with an uncovered short leg, which is why rolling a short spread is impossible and PRD §8.2 already defers it.

Order cost basis is `maintenance_margin + net_price × multiplier`, computed under the universal-spread rule across the whole portfolio.

### There is no realized P&L anywhere

**Spec.** The `FILL` shape below is *not* probed — this account has no fills. `GET /v2/account/activities/FILL` returns `activity_type`, `id`, `order_id`, `order_status`, `symbol`, `side`, `qty`, `cum_qty`, `leaves_qty`, `price`, `transaction_time`, `type` (`fill` | `partial_fill`). `page_size` maxes at 100.

**No P&L field, and no `position_intent`.** `side` is buy/sell; `ActivityItem.action` is BTO/STC/STO/BTC. `position_intent` lives on the *order*, so every ledger row needs a fill→order join.

`ActivityItem.pnl` / `pnlPct` and all three Activity header cards therefore have no source. Alpaca does not compute this.

### An option expiring is not a fill, and it is not even the same schema

`OPEXP` (expiration), `OPASN` (assignment) and `OPEXC` (exercise) are their own activity types, alongside `OPTRD`, `OPCA` and `OPCSH`. Fees arrive as `FEE` with sub-types `ORF`, `OCC`, `TAF`, `CAT`, `COM`, `REG`. Cash movements are `TRANS` / `CSD` / `CSW`.

Phase 1's ledger has four statuses and no concept of expiry — so the most common way an option position ends has no render path, and it is a realized loss.

**Probed, and the original spec described only half the surface.** The activities endpoint returns **two different object shapes**. The one `JNLC` row on this account came back as `{id, activity_type, date, created_at, net_amount, description, status, currency}` — **no `symbol`, no `qty`, no `price`, no `side`**.

The OpenAPI document confirms the split: the `200` response is `oneOf [TradingActivities, NonTradeActivities]`, chosen by the `activity_type` in the path. `NonTradeActivities` is `{activity_type, activity_sub_type, created_at, currency, cusip, date, group_id, id, net_amount, per_share_amount, qty, status, symbol}`, with `symbol` and `qty` both documented as *"not present for all activity types"* — exactly what the live `JNLC` demonstrated.

Two fields appear in live responses and in Alpaca's own doc examples but **not** in the published `NonTradeActivities` schema: `description` (seen live) and `price` (seen on the `OPTRD` examples below). Model the non-trade branch permissively; do not assume the published schema is complete, because it demonstrably is not.

Ingestion needs **two branches discriminated on `activity_type`, not one**. And the non-trade branch has **no `order_id` at all** — `group_id`, *"ID used to link activities who share a sibling relationship"*, is the only linkage a non-trade activity gets. That contradicts the fee-attribution design below, corrected there.

### An option event carries no price of its own

**Verified against Alpaca's documented examples, not against this account**, which has never held an option. Marked accordingly, and it is the first thing to re-check against a real event.

An option event arrives as a **pair** of rows — the event on the contract, and an `OPTRD` on the underlying carrying the money:

- **Exercise** — `OPEXC` on `AAPL230721C00150000`, `qty: "-2"`, `net_amount: "0"`; paired `OPTRD` on `AAPL`, `qty: "200"`, `price: "150"`, `net_amount: "-30000"`.
- **Assignment** — `OPASN` on the contract, `qty: "2"`, `net_amount: "0"`; paired `OPTRD` on `AAPL`, `qty: "-200"`, `price: "150"`, `net_amount: "30000"`.
- **OTM expiry** — a lone `OPEXP`, `qty: "-2"`, `net_amount: "0"`, and nothing else. The position is flattened.

Three consequences, each contradicting something the Design section originally said:

1. **`net_amount` on the option row is zero.** The design said `OPASN` and `OPEXC` *"close at intrinsic"* — but there is no intrinsic value anywhere on that row. It has to come from the paired `OPTRD`'s `price`, which is the strike, or from the strike parsed out of the OCC symbol. The matcher cannot read a close price off the event row itself, and a matcher that trusts `net_amount` books every exercise as a total loss.
2. **`qty` is signed on non-trade rows** — `-2` when contracts leave a long, `+2` when a short is assigned away — where `FILL` rows carry an unsigned `qty` and a separate `side`. Two conventions in one ingest path.
3. **An ITM expiry never produces `OPEXP`.** Alpaca auto-exercises ITM contracts absent a DNE instruction, so ITM expiry arrives as the `OPEXC` pair. `OPEXP` is the OTM case *only* — which makes *"`OPEXP` closes remaining lots at zero"* correct, but for a reason the original spec did not state, and it leaves the ITM path as the underspecified one.

**Unverified, and it is a schema risk rather than a logic one:** Alpaca's doc examples give both rows of a pair the *same* `id`. That is probably a copy-paste artifact, but if it is real then `fill(activity_id UNIQUE)` silently drops the second row of every option event. Check it against the first real event; `group_id` is the field intended for the linkage.

### The account object has no settlement breakdown

**Probed.** `GET /v2/account` returns `cash`, `buying_power`, `effective_buying_power`, `options_buying_power`, `non_marginable_buying_power`, `regt_buying_power`, `equity`, `last_equity`, `long_market_value`, `short_market_value`, `position_market_value`, `portfolio_value`, `multiplier`, `initial_margin`, `maintenance_margin`, `last_maintenance_margin`, `sma`, `accrued_fees`, `intraday_adjustments`, `pending_reg_taf_fees`, `balance_asof`, `crypto_tier`, `options_approved_level`, `options_trading_level`, `admin_configurations`, `user_configurations`, `status`.

Three corrections to the list the original spec took from the published docs:

- **`pending_transfer_in` and `pending_transfer_out` are absent.** Nothing reads them, so nothing breaks — but they must not appear as required fields on a Pydantic model, which is how an absent field becomes a 500 on every account request.
- **`options_approved_level` and `options_trading_level` are integers, not strings** — both `3` here. Every *other* numeric field on the object is a string, so a parser that maps the whole object through `Decimal(str)` uniformly will break on exactly these two. Confirms the account is Level 3 without hardcoding it.
- Ten fields were present and unlisted: `effective_buying_power`, `position_market_value`, `portfolio_value`, `intraday_adjustments`, `pending_reg_taf_fees`, `last_maintenance_margin`, `balance_asof`, `crypto_tier`, `admin_configurations`, `user_configurations`. **`position_market_value` is the one worth naming**: §8.6 defines total equity as *"cash + the market value of open positions"* and the broker supplies the second term directly, so the reconciliation on screen can be checked against the broker's own figure rather than only against a sum over positions.

**No `settled`, no `unsettled`, no settled-cash field of any kind** — now confirmed on the live object rather than merely absent from the docs. This independently validates decision 9. Two doc searches had found no settlement concept either. **Already actioned**: the split was removed from the codebase and PRD §8.6 on 2026-09-10, with the reasoning recorded there.

Three useful positives, all confirmed live: `multiplier` tells you margin class (1 cash / 2 Reg T / 4 PDT), `options_trading_level` reports the real approval level, and `last_equity` is equity at the previous close — a day change for free.

**`multiplier` is `'4'` on this paper account, which makes it a PDT margin account.** That contradicts on-screen text: PRD §8.6 states *"Paper is a margin account at 2× cash"*, and at 4× that sentence is wrong in the direction that **overstates capacity**. The Account page must read `multiplier` and say what it finds rather than asserting a number — the whole point of §8.6's "the page says why" is that the figure be true.

The wider consequence needs no decision but should be understood: paper is a **weaker rehearsal for Phase 7 Cash than the PRD's framing implies**. §2 presents Paper/Cash as *"which Alpaca account keys are in use"*, as though the accounts differed only in whose money is at stake; they also differ in margin class, and a strategy sized comfortably against 4× buying power is untestable on a cash account with none. What holds the framing together is that `options_buying_power` — the figure that actually binds an options trader, and which is never the margin figure because options are not marginable — is supplied on both and is the right thing to size against on both. Say that on the page instead of "2×".

### Chain data is split across two endpoints, and on Basic neither one is complete

**Probed, and this is where the published spec was most wrong.**

`GET /v1beta1/options/snapshots/{underlying}` returns per contract: `latestQuote` (`bp`/`ap`/`bs`/`as`), `latestTrade` (`p`), `dailyBar` (`v` = volume), `minuteBar`, and `prevDailyBar` (`c` = previous close) where one exists. Server-side filters: `type`, `strike_price_gte`/`lte`, `expiration_date`(`_gte`/`_lte`), `root_symbol`. Limit defaults to 100, maxes at 1000, and **applies to total data points rather than per symbol**; paginate on `next_page_token`. All of that is confirmed.

**`impliedVolatility` and `greeks` are both `None` on `feed=indicative`.** The original spec claimed both, taken from the OpenAPI document — where they *are* real properties of `option_snapshot`, but, read carefully, **optional ones**: neither appears in any `required` list, and both are described as *"calculated using the Black-Scholes model"*. Alpaca derives them rather than receiving them, and does not serve them on the free feed. The example response in the docs shows them populated, which is how this got into the spec.

**`feed=opra` returns HTTP 403 `{"message": "OPRA agreement is not signed"}`.** That is an *agreement* error, not a plan or entitlement error, and the wording is the entire finding — Alpaca's own docs say only that *"OPRA feed is only available to subscribed users"* and are silent on whether the agreement is a separate step. See *Open questions*; do not assume it either way.

This is the most consequential correction in this amendment, because two shipped behaviours rest on the missing fields:

- Markets' chain renders an **IV column**, and PRD §8.4 lists "highest IV" as a named screen.
- The fixture's day-change model **scales the underlying's move by delta**, which `mockData.test.ts` pins as an invariant. Without a delta there is no model.

Neither has a source on Basic. The one mitigating fact is the one the OpenAPI document gives away: Alpaca's greeks are *computed*, not observed, so computing them locally reproduces the method rather than approximating a measurement.

The snapshot also carries **no open interest**, as originally stated. That comes from `GET /v2/options/contracts`, which returns `open_interest` and `open_interest_date`, `close_price`, `strike_price`, `expiration_date`, `type`, `style`, `root_symbol`, `multiplier`, `size`, `deliverables`, `status`, `tradable`, `name`. Limit maxes at 10,000.

**But `open_interest` is `null` on every contract sampled** — including established Oct 2026 expiries, so this is not a not-yet-populated new listing. `open_interest_date` and `close_price` are null too. So Markets' "highest open interest" screen is not merely a join across two endpoints: on this plan it is a join that yields a column of nulls, and the screen ranks on nothing. That needs a decision rather than a designed null state.

The **structural** fields on the same endpoint — `strike_price`, `expiration_date`, `type`, `root_symbol`, `multiplier`, `size` — are populated. The adjusted-contract detection below is therefore unaffected, which matters more than the screen does: it is the one that feeds rule 4.

### The adjusted-contract warning has an exact answer

**Spec, and the fields it depends on are probed present.** `/v2/options/contracts` returns `multiplier` **and** `size` as separate fields, with the spec stating explicitly that `size` *"should not be used as a multiplier"*. It also returns `root_symbol` and, on request, `deliverables`. Unlike `open_interest` on the same endpoint, all four come back populated.

Detection is `root_symbol != underlying_symbol`. Sizing and P&L read `multiplier` per contract, never the frontend's `CONTRACT_MULTIPLIER = 100`.

### Not verified

Carried forward as implementation-time checks rather than assumptions:

- **Whether the 30-symbol websocket cap is per-stream or global.** The option and stock streams are separate connections. Designed for the pessimistic reading (30 total) behind a config flag.
- Market cap and average daily volume are absent from Alpaca (confident, unverified this session). `avgVolume` is computable from daily bars; market cap comes from Finnhub `/stock/profile2` → `marketCapitalization`.
- The stock snapshots endpoint's exact field shape.
- Whether `order_class: oco` is accepted for *options* — carried over unresolved from the Open Positions spec. Phase 6's problem, not this one.
- `non_marginable_buying_power` semantics on a cash account.
- Whether the same `id` really appears on both rows of an option-event pair, which would make `fill(activity_id UNIQUE)` drop half of every event. Check against the first real one.
- Whether `open_interest` is null because of the plan or because Alpaca populates it only after a settlement cycle this account has never had.

**Resolved since the original list:** the MCP 401 is diagnosed — the server holds non-paper keys, and the `.env` keys work — so the provider is no longer being built blind. The account, contracts, snapshot, portfolio-history and activities shapes above are probed.

---

## Open questions

Added by the 2026-09-10 amendment. These are the places where the probe found
an absence rather than a mismatch, so no amount of care in the mapper resolves
them. Each blocks a specific shipped behaviour, and each is a judgement about
what the terminal should say when it does not know something.

### 1. IV, greeks and open interest have no source on Basic

Blocks: the chain's IV column, PRD §8.4's "highest IV" and "highest open
interest" screens, and the delta-scaled day-change model that
`mockData.test.ts` currently pins.

Four ways out, and they are not mutually exclusive:

- **Compute IV and greeks locally** from the mid price with Black-Scholes.
  The OpenAPI document describes Alpaca's own values as *"calculated using
  the Black-Scholes model"*, so this reproduces their method rather than
  approximating a measurement — which is a much stronger position than it
  first sounds. Needs a risk-free rate and a dividend assumption, and it
  does nothing for open interest, which is a fact about the market that
  cannot be derived from a price.
- **Check the OPRA agreement first.** The 403 says *"OPRA agreement is not
  signed"*, which is an agreement, not an entitlement. If signing it in the
  Alpaca dashboard is free, all three fields arrive and this question
  disappears. Cheapest thing to try and it should be tried before any code
  is written.
- **Mark the columns unavailable**, in the same words as §8.5's fixture
  markers. Honest, and consistent with the rule that a fluent non-answer is
  worse than an admitted gap — but two named screens stop working.
- **Drop the columns and the screens** until a plan or provider supplies
  them.

The one option that is *not* open is inventing values. An IV column that
renders a plausible number nobody computed is precisely the failure §8.5
names when it says a table of invented numbers reads as invented.

### 2. The ledger has no data to be validated against

Blocks: confidence in decisions 4 and 5, not the decisions themselves.

This account has zero fills. Decision 4's stated argument against matching
only Corollary-placed trades — that it *"leaves a paper account's existing
history blank"* — is moot, because the history is blank either way. The
decision still stands on its other leg (lifetime P&L is the truest thing on
the Activity page), but it is now standing on one leg and the spec should
not pretend otherwise.

Three ways to get validation data:

- **Place a handful of paper trades by hand** — including one multi-leg
  order and one contract held to expiry — purely to generate real fills.
  A few dollars of fake money buys recorded fixtures with real shapes, and
  it is the only option that tests the grouper against an actual `mleg`
  order rather than an imagined one. Slowest, because expiry takes a week.
- **Author fixtures from Alpaca's documented examples**, which is what the
  option-event section above already had to do. Fast, and it proves the
  arithmetic is self-consistent while proving nothing about the shapes.
- **Defer the ledger to a later phase** and ship Phase 2 with the Activity
  header cards marked unavailable. Contradicts decision 4 and lowers the
  bar of *"you'd open it in the morning and learn something true."*

### 3. Paper is a 4× PDT margin account, and the PRD says 2×

Not a question so much as a correction that needs making somewhere. PRD
§8.6 asserts *"Paper is a margin account at 2× cash"*; this account reports
`multiplier: '4'`. The number on screen must come from the account object
rather than from prose, and the prose should go.

The deeper point, recorded here rather than acted on: paper is a weaker
rehearsal for Phase 7 Cash than §2's *"which Alpaca account keys are in
use"* framing implies, because the two differ in margin class as well as in
whose money is at stake. `options_buying_power` is the figure that binds an
options trader on both, and is the right thing to size against on both.

---

## Decisions

Nine decisions, taken 2026-09-09/10. Each records what it rules out, because the alternative is usually the thing someone reaches for later.

### 1. One process

`uv run uvicorn corollary.api:app` is the whole app. The scheduler and streams are asyncio tasks started in the FastAPI lifespan.

This resolves PRD §12. SQLite gets a single writer for free, avoiding the WAL-plus-single-writer discipline that both docs flag `database is locked` as the failure mode of. More importantly the code that detects a dropped Alpaca connection is the same process that answers `/api/engine/state`, so there is no window in which the API reports *running* because it has not heard otherwise.

`corollary/engine/` and `corollary/api/` stay separate modules, so the two-process split §3 draws remains a deployment change rather than a refactor.

Rejected: two processes. Real crash isolation, but Basic allows one websocket per account, so the API would have to proxy quotes through the engine — an IPC channel is required either way, and the DB contention is not.

Side effect worth naming: `uvicorn --reload` restarts the engine on every edit, landing you in halted-Paper. Under rules 5 and 9 that is the correct posture, not a cost.

### 2. Every write control is inert

Close, Add, Attach/Edit exit, Working Orders' Cancel, Flatten and Execute-recommendation all render disabled with a one-line reason naming Phase 6.

There is no order path in the codebase at all, so rule 1 is satisfied structurally rather than by discipline.

Rejected: cancel-only, which is defensible — a cancel is not an order — but puts the first broker write before the risk manager exists. Also rejected: pulling manual close forward, which contradicts §11's boundary and the rule that Phases 6–7 get tests before features.

Halt and Resume *are* real engine state, because rule 9's dead-man's switch fires on a real connection in this phase.

### 3. Config writes are live

Risk limits, feed selection and notification routing persist to SQLite with §8.7's audit log. These are config, not orders — rule 1 untouched — and they are the database's natural first customers, so Phase 6's risk manager reads a table that already exists.

API key presence, feed status and options level must be server-backed regardless: only the server can see the environment.

### 4. Build the FIFO realized-P&L ledger

Match closing fills against opening fills FIFO per contract symbol, including expiry and exercise. Real work, `Decimal` throughout, and it needs tests — this is arithmetic that can be wrong about money.

Rejected: em dashes. `/v2/account/portfolio/history` gives the account-level equity curve free, so the Dashboard chart would still work, but avg win and avg loss are exactly the per-trade figures that would go missing. Lifetime P&L is the truest thing on the Activity page and Phase 2's bar is learning something true.

Also rejected: matching only Corollary-placed trades, which leaves a paper account's existing history blank.

PRD §8.6's *"Corollary keeps no ledger"* is scoped to cash transfers. This is genuinely a ledger and the PRD now says so.

### 5. Group multi-leg only where an mleg order proves it

Join legs to the historical mleg order that opened them. Group where the evidence exists; leave unexplained legs as their own labelled rows.

Evidence-based rather than inferred, so it never invents a spread that is not there, and it avoids reporting a defined-risk credit spread as an undefined-risk naked short.

Rejected: per-leg rows now and grouping in Phase 6 — honest, but the payoff curve, max-loss and risk class are wrong or absent for any spread already held, and the position model gets built twice. Also rejected: a heuristic on same-underlying/same-expiry/offsetting-sides, which is wrong on iron condors, ratio spreads, and any two unrelated positions that happen to rhyme.

### 6. The equity curve is Alpaca's, with t₀ marked

Draw `/v2/account/portfolio/history` and mark where Corollary started running.

Reading the broker's own record is not reconstruction — nothing is simulated, and §8.6's principle is that Alpaca is the source of truth. The t₀ marker preserves what §8.1's *"no pre-Corollary reconstruction"* line actually protects: the chart sits beside a strategy win rate and must not claim credit for manual trading.

Requires the PRD §8.1 amendment listed below.

### 7. Finnhub market cap, early

One field from `/stock/profile2`, cached daily. `FINNHUB_API_KEY` is already in `.env.example` and Finnhub is already §7's source for calendar and consensus, so this is a planned dependency arriving one phase early rather than a new one.

Rejected: a column of em dashes (the null path is designed, but one of the named stock screens stops working), and deleting the column to re-add it a phase later.

### 8. Per-page fixture markers

News, Research and Settings' sentiment readout carry a small marker in the same words as Research's existing scripted-chat marker.

§8.5's argument that *"a table of invented numbers reads as invented"* held when everything was a fixture. It is much weaker when the page next door is real — the News sentiment composite in particular reads as computed.

Settings mixes real and mock within one page, so its markers sit on the affected panels rather than the page title.

### 9. Settled vs unsettled is removed

Actioned 2026-09-10. See PRD §8.6 for the reasoning and the consequence to weigh before reinstating.

---

## Three rule reinterpretations, approved

### The vendor surface gains a read half

CLAUDE.md names two files as the entire Alpaca surface. Reading positions, orders, activities and balances is neither market data nor order placement, so the rule does not say who owns it. The trading API *is* the broker, so `BrokerInterface` splits:

- **`BrokerAccount`** — account, positions, orders, activities. Implemented in Phase 2 by `engine/execution/alpaca.py`.
- **`BrokerExecution`** — submit, cancel, replace. **Does not exist until Phase 6.**

API routes depend on `BrokerAccount` only, so `submit_order` is not in a type they can reach. That keeps rule 1 structural rather than disciplinary. Rule 2 is untouched: the backtest worker still gets a scrubbed environment and `SimBroker`.

### `Decimal` serializes as a JSON number

Money is `Decimal` everywhere it is *computed* — Alpaca's strings parse straight to `Decimal` on ingest, DB columns are `Numeric`, the matcher and engine are `Decimal` throughout. The API boundary is a display boundary and serializes to a JSON number.

Chosen on ease, as directed: the alternative converts 34 frontend files and every `format.ts` helper to strings for a single-user terminal whose largest figure is five digits. The constraint this carries: **client-side money arithmetic is display-only.** `account.ts` sums position value and equity; the server computes both authoritatively, and the client's version is the live estimate between refreshes, marked from the stream — which is what §8.6 already describes.

### `store.tick()` is simplified, not deleted

Its three jobs separate:

- **Re-marking** moves to the WebSocket handler. Same store state, different writer.
- **Filling working orders** goes. Alpaca fills; a `trade_updates` event triggers a refetch.
- **Triggering attached exits** goes. There is no execution.

The quote state it owns — `underlyings`, contract marks, `lastTickAt` — stays exactly where it is, so every component reading `underlyings[symbol]` is unchanged.

The mock-broker logic is **preserved in the test suite**, not deleted: `orderWouldFill` and `exitTrigger` in `orders.ts` are pure and stay, and their tests keep exercising them. What goes away is the store calling them against a book that is now real. A simulated fill applied to a live position is the single most dangerous thing this phase could ship.

---

## Design

### Module layout

```
corollary/
├── api/
│   ├── app.py            # FastAPI app, lifespan, routers
│   ├── deps.py           # DI: provider, broker, session, engine handle
│   ├── schemas.py        # Pydantic response models — the API contract
│   └── routes/
│       ├── account.py    # account, portfolio history, transfers
│       ├── positions.py  # logical positions, working orders
│       ├── activity.py   # the ledger — paginated, searchable, filterable
│       ├── markets.py    # stocks, chains, underlyings
│       ├── engine.py     # state, halt, resume
│       ├── settings.py   # limits, feeds, routing, keys, audit, sources
│       └── ws.py         # WS /api/stream
├── engine/
│   ├── runtime.py        # EngineRuntime: lifecycle, halt state, watchdog
│   ├── scheduler.py      # asyncio tasks: poll, ingest, EOD roll
│   ├── stream.py         # the 30-symbol budget manager
│   ├── ledger.py         # FIFO matcher — pure
│   ├── grouping.py       # mleg reconstruction — pure
│   └── execution/
│       ├── interface.py  # BrokerAccount + BrokerExecution
│       └── alpaca.py     # AlpacaBroker — BrokerAccount only in Phase 2
├── data/providers/
│   ├── interface.py      # MarketDataProvider
│   └── alpaca.py         # AlpacaProvider
└── db/
    ├── models.py
    ├── session.py
    └── migrations/       # Alembic, wired for the first time
```

### Feeds and budgets

| Feed | Mechanism | Cadence | Scope |
|---|---|---|---|
| Position quotes | option WS + stock WS + `trade_updates` | push | open contracts and their underlyings |
| Market snapshots | REST snapshots | 2s | Markets' visible universe |
| Account / positions / activity | REST | 15s, and on demand | active account |

**Two token buckets, not one.** `data.alpaca.markets` and `paper-api.alpaca.markets` each carry their own 200/min. Budget: a 2s market poll is 30/min, the account trio at 15s is 12/min, chains are on demand. Comfortable headroom.

**The symbol budget is enforced, not noted.** Grouped multi-leg reaches 32 option symbols at eight positions before underlyings are counted. `engine/stream.py` holds a priority-ordered subscription list — position contracts, then underlyings, then recommended trades in Phase 4 — drops the tail, logs it, and surfaces *"N symbols not streamed"* to the UI. Never silently.

### Database

SQLite, WAL, one writer. Ten tables.

- `risk_limit(key, value Numeric)` · `data_feed(key, value)` · `notification_route(event, channel, enabled)` — typed separately rather than one key/value table, because the limits need `Numeric`.
- `audit_log(id, at, category, field, previous_value, new_value)` — spans all three, per §8.7's *"one log rather than three"*.
- `engine_state(id=1, halted, halted_reason, halted_at, t0)` — singleton.
- `fill(id, account, activity_id UNIQUE, order_id, symbol, side, position_intent, qty, price, at)` — raw activities. Needed because `page_size` maxes at 100 and re-fetching all history per request is untenable.
- `realized_trade(id, account, symbol, opened_at, closed_at, qty, open_price, close_price, pnl Numeric, pnl_pct Numeric, close_kind)` — matcher output. `close_kind` ∈ `fill` | `expiry` | `exercise` | `assignment`.
- `mleg_group(id, account, order_id, opened_at, net_price)` + `mleg_leg(group_id, symbol, ratio, side, position_intent)` — the grouping evidence.
- `notification(id, at, event, severity, account, title, body, read_at, dismissed_at)` — engine events only this phase.

No `equity_snapshot`: the curve comes from Alpaca and the marker from `engine_state.t0`.

Ingestion runs on startup and on an interval — pull activities newer than the last `transaction_time`, upsert, join to orders for `position_intent`, re-run the matcher incrementally. Idempotent on `activity_id`.

### The FIFO matcher

Pure function, fill sequence → realized trades. No I/O, `Decimal` throughout.

An open-lot queue per contract symbol. `*_to_open` pushes a lot; `*_to_close` pops FIFO, emitting a trade per matched slice. Long P&L is `(close − open) × qty × multiplier`; short inverts. `multiplier` is per contract from the contracts endpoint, cached.

`OPEXP` closes remaining lots at zero — a full loss on a long, the full credit kept on a short. Per the option-event finding above, `OPEXP` is the **OTM case only**: Alpaca auto-exercises ITM contracts absent a DNE instruction, so an ITM expiry arrives as an `OPEXC` pair and never reaches this branch.

`OPASN` and `OPEXC` close at the strike, **read from the paired `OPTRD` row's `price` or parsed from the OCC symbol — never from the event row's `net_amount`, which is zero.** A matcher that trusts `net_amount` here books every exercise as a total loss. They emit a flag; Corollary is not a stock app, so the resulting shares are named, not tracked.

The matcher also normalises two conventions into one: `FILL` rows carry an unsigned `qty` with a separate `side`, while non-trade rows carry a **signed** `qty` and no `side` at all. Normalise on ingest, so the matcher itself sees one convention.

Fees attribute by `order_id` where one is present. **Non-trade activities have no `order_id`** — `group_id` is the only linkage they get — so fee attribution for expiry, assignment and exercise runs through `group_id`, and anything still unattributed is reported separately rather than dropped.

`pnl_pct` denominates on cost basis — `open_price × qty × multiplier` — so a short's basis is the credit received, matching `orders.ts`'s negative `openUnitValue`.

### Multi-leg grouping

Pure. Broker positions plus mleg history → logical positions.

Each historical mleg order proposes a group. A group is **live** only if every leg still holds a nonzero position on the expected side, with quantities consistent with the order's ratios. Live groups become one logical position with `legs[]` populated. Everything else is a single-leg position labelled `ungrouped`.

A partially-closed spread is not live — its survivors fall back to ungrouped rows, because that spread genuinely no longer exists. Direction comes from the order's net price: debit long, credit short.

Corollary's own five `Position` fields have no Alpaca source, and in this phase that is correct rather than a gap: nothing Corollary opened exists, so `strategyId` is null, `managedExit` and `attachedExit` are null, and every position is legitimately detached. The UI already renders that state. `valueHistory` is derived from the contract's daily bars between the opening fill's date and today — the entry date comes from `fill`, not from the position.

### Frontend migration

Two mechanical steps first, no behaviour change:

1. **`web/src/lib/types.ts`** — extract every type, label map and constant out of `mockData.ts`, which keeps only fixtures. 34 production files repoint. `npm run typecheck` proves it. Without this, Phase 2 ships 2,606 lines of seeded fixtures in the bundle.
2. **`web/src/lib/api.ts`** — typed client, one function per endpoint. Vite dev proxy `/api` → `127.0.0.1:8000`.

Then the state split:

| State | Owner |
|---|---|
| positions, activity, working orders, account, chain, stocks, settings, audit, notifications | TanStack Query |
| live quotes, `lastTickAt`, `lastPollAt`, connection status | Zustand, written by the WS |
| theme, accountMode, executionMode, palette, filters, expanded row | Zustand, unchanged |
| strategies, chat, dispositions, news | Zustand + fixtures, unchanged |

Quotes stay in Zustand deliberately: a WS push is not a query, and `setQueryData` per tick fights the cache's staleness model. The existing `tick`/`pollMarkets` split already draws this line correctly.

Query keys are account-scoped — `['positions', accountMode]` — so switching books refetches rather than rendering the other one, preserving the invariant `Record<AccountMode, …>` enforces today.

`MARKET_TODAY` becomes a market-calendar clock; `exchange-calendars` is already a dependency. `lastTickAt === null` finally means what §8.2 predicted: skeletons while the opening snapshot is in flight.

Cash mode's toggle disables with a stated reason while `ALPACA_LIVE_*` is absent. The confirm dialog has no balance to name, which is its own argument.

### Engine runtime and the dead-man's switch

`EngineRuntime` owns the sockets and a watchdog. Ninety seconds without a message or a successful poll, or a WS close, triggers `halt(reason)`: persist to `engine_state`, emit a `critical` notification, fire Discord if routed.

`resume()` is reachable only from `POST /api/engine/resume`. Never internally, and never automatically from the client — it reconnects the *socket*, never the halt.

Cold start comes up **halted until the opening snapshot succeeds**, and always Paper. `t0` is written on the first ever start.

Halting stops nothing this phase, because nothing trades. The state, the notification and the explicit-resume requirement are real and tested regardless — rule 9 is the one item here that cannot be retrofitted. The risk-manager heartbeat has no producer yet, so the watchdog ships with two conditions and one documented as inert.

---

## Testing

**Backend.**

- `ledger.py` — long and short round trips both directions, partial closes, FIFO across two lots at different prices, expiry-worthless both directions, `multiplier != 100`, fee attribution. Plus the reconciliation invariant: `Σ realized + unrealized ≈ account P&L` within fees. Also the two conventions the probe surfaced: a signed non-trade `qty` normalises to the same lot movement as an unsigned `FILL` `qty` plus `side`, and an `OPEXC` pair closes at the strike rather than at `net_amount`'s zero.
- **Where the fills come from is itself a decision** — this paper account is empty, so every ledger test is authored rather than recorded. That is fine for the arithmetic and worthless for the shapes: a hand-written fixture proves the matcher is self-consistent, never that it matches what Alpaca sends. See *Open questions*. Whatever the answer, the first real fill is a reconciliation checkpoint, not a formality.
- `grouping.py` — two-leg spread groups; iron condor groups; one leg closed ungroups; ratio mismatch does not group; **two unrelated same-expiry positions do not group**, which is what proves this is not a heuristic.
- Provider — recorded Alpaca responses as fixtures, no live calls. Feed-name resolution from the three env vars. Rate limiter. Adjusted contracts filtered on `root_symbol`.
- Watchdog — halts on timeout, halts on WS close, never resumes itself.
- API — schema round-trip against the TS types.
- `uv run pytest -m risk` still collects. `mypy --strict` clean.

**Frontend.**

- The type extraction is proven by `npm run typecheck`.
- Page tests keep their fixtures as *mocked API responses* rather than direct imports — which is why the type split comes first.
- New: account-scoped query keys do not leak between books; the stale pill; disconnected renders halted and never auto-resumes.

---

## Doc amendments

- **PRD §8.1** — equity curve is Alpaca's, with a t₀ marker.
- **PRD §12** — one process, resolved.
- **PRD §8.2** — the realized-P&L ledger exists; expiry and assignment are ledger states.
- **PRD §8.6** — *"keeps no ledger"* scoped to cash transfers. *(Settled/unsettled: done 2026-09-10.)* Also: **drop *"Paper is a margin account at 2× cash"*** — this account reports `multiplier: '4'`, and the figure must be read from the account object rather than asserted in prose. Point the page at `options_buying_power`, which is what actually binds an options trader and is supplied on both account types.
- **PRD §8.4** — the "highest IV" and "highest open interest" screens depend on fields Basic does not serve. Whatever *Open questions* §1 resolves to, §8.4 has to say it.
- **CLAUDE.md** — the vendor surface gains `BrokerAccount`; layout gains `engine/{ledger,grouping,runtime,stream}.py`, `api/routes/`, `api/schemas.py`; `CONTRACT_MULTIPLIER` is a fixture default and real multipliers are per contract; options level is read from the account, not hardcoded to 3.

---

## Order of work

0. **Check whether signing the OPRA agreement is free.** Not code, and it
   gates *Open questions* §1 — if it unlocks IV, greeks and open interest,
   several downstream steps get simpler and nothing needs computing locally.
   Do it before writing any Markets code.
1. Type extraction and vite proxy — pure refactors.
2. DB and Alembic wired; three config tables, audit log, engine state. Settings goes server-backed.
3. `MarketDataProvider` + `AlpacaProvider`: quotes, snapshots, bars, chain, contracts. Rate limiter, feed config.
4. `BrokerAccount` + `AlpacaBroker` read surface.
5. Fill ingestion, FIFO matcher, `realized_trade`.
6. Multi-leg grouping.
7. API routes and schemas; frontend query migration page by page — Account, Activity, Dashboard, Markets.
8. WS fan-out, `EngineRuntime`, watchdog; simplify `store.tick()`.
9. Finnhub market cap; fixture markers.
10. Doc amendments.

---

## Out of scope

No `submit_order`, no `RiskManager.approve()` body, no `BrokerExecution`. No `SimBroker`, Parquet, DuckDB or backtest worker. No news, sentiment, calendar, consensus or social data. No strategies, scanner, LLM layer or indicator whitelist. No rolling, trailing stops, or greeks in the ticket — all three already deferred by §8.2.
