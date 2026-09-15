# Phase 2: read-only real data

**Date:** 2026-09-10
**Status:** Approved, not yet implemented. **Amended 2026-09-10** after probing the real paper account. Three claims taken from Alpaca's published specs turned out to be wrong on this plan, and two of them leave the Markets page blocked on a decision. See *What the probe reached, and what it could not* and *Open questions*.

**Amended 2026-09-14.** Five things — four decisions, 17–20, and one verified constraint. One is a **factual correction with shipped code resting on it**: the 30-symbol websocket cap is the *equity* stream only, and the option stream carries a separate 200-quote budget, so `engine/stream.py` is currently rationing a resource that is not scarce. Two are cadence decisions taken by the account holder — an adaptive 400ms/5s Markets poll alongside a push-only position feed, and pulling the Algo Trader Plus subscription forward from Phase 6 to Phase 4. **Decision 20** is what the cadence becomes on the upgraded plan, written now so the upgrade is a config change and a list of deletions rather than a redesign under time pressure; the constraint is the series ceiling's fetch/serve asymmetry and `1H`'s inability to represent the session open, both out of the regular-trading-hours work committed the same day. See *The websocket cap is two budgets, not one* and *The series ceiling counts served points, not fetched bars* under Constraints, the rewritten *Feeds and budgets*, and decisions 17 through 20.
**Order of work rewritten 2026-09-14.** Every remaining unit is now its own
entry with a status, its dependencies and its files, and landed steps name the
commit that landed them. Step 8's six sub-steps and the work decisions 17 and 18
created are tracked there rather than in `.claude/scratch/`, which `.gitignore`
excludes from the repo. See *Order of work*.
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

**Probed.** Seven logical positions produced **eleven** rows — four verticals contributing two each, three singles contributing one — which is the grouping problem in its plainest form. Two fields not in the list above are present: `asset_marginable` and `exchange` (empty string on options). And a short leg reports `qty: "-1"` **alongside** `side: "short"`, with **`cost_basis` and `market_value` both negative** (`-4155`, `-4250` on the AMD 470 put). That independently confirms `orders.ts`'s rule that a short's `openUnitValue` is negative because a credit is a liability — the broker agrees, in its own numbers.

Consequence: `Position.legs[]`, the payoff curve, max-loss and the DTE column all assume one logical position. A short leg rendered alone reports as an *undefined-risk* naked short, so a defined-risk credit spread would state the wrong risk class — the exact failure CLAUDE.md rule 4 exists to prevent.

### Multi-leg orders exist; multi-leg positions do not

**Spec.** Not probed — this account has never placed an order. `order_class: "mleg"` with a `legs[]` array of `{symbol, ratio_qty, side, position_intent}`. Two rules constrain reconstruction:

- Leg ratios must be in simplest form — the GCD across `ratio_qty` values must be 1.
- Every leg must be covered within the same order. Alpaca rejects an mleg order with an uncovered short leg, which is why rolling a short spread is impossible and PRD §8.2 already defers it.

Order cost basis is `maintenance_margin + net_price × multiplier`, computed under the universal-spread rule across the whole portfolio.

### There is no realized P&L anywhere

**Probed 2026-09-10, second pass.** Seven recommendation-shaped orders were
placed on the paper account (four `mleg` verticals, three single-leg), and one
single-leg long was closed for a round trip. The `FILL` shape is now observed
rather than assumed, and it corrected three things.

**`side` has three values, not two.** Observed: `buy` ×7, `sell_short` ×4,
`sell` ×1. The original claim that *"`side` is buy/sell"* is wrong, and the
error is not cosmetic — `sell_short` is an opening sale (STO) while `sell` is
a closing one (STC), so `side` alone distinguishes those two. It does **not**
distinguish `buy`: BTO and BTC are both `buy`, which is what makes the
fill→order join for `position_intent` mandatory rather than merely convenient.

**A fill's `order_id` is the *leg* id, not the parent order's.** For a simple
order they coincide. For an `mleg` order each leg is a full order object with
its own `id`, and that is what lands on the fill; the parent id appears
nowhere on it. Reaching the parent requires `GET /v2/orders?nested=true` and a
leg-id → parent-id map built from `legs[]`. Decision 5's *"join legs to the
historical mleg order that opened them"* is therefore a **two-hop join**, and
a one-hop implementation silently groups nothing — every fill would look like
a single-leg order that happens to exist.

**The activity `id` is composite**: `20260910131125598::68cda3e9-…`, a
timestamp concatenated with a UUID. It sorts chronologically as a string,
which is convenient for the `fill(activity_id UNIQUE)` upsert and for
resuming ingestion, but it is not a bare UUID and must not be typed as one.

The round trip — IWM 280P bought at `8.21`, sold at `8.14` — is the first
real realized trade on the account: a **−$7.00** loss at a 100 multiplier, and
the arithmetic the FIFO matcher must reproduce exactly.

**Spec (unchanged, still unprobed for the fields below).** `GET /v2/account/activities/FILL` returns `activity_type`, `id`, `order_id`, `order_status`, `symbol`, `side`, `qty`, `cum_qty`, `leaves_qty`, `price`, `transaction_time`, `type` (`fill` | `partial_fill`). `page_size` maxes at 100.

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

### The websocket cap is two budgets, not one — verified 2026-09-14

**Spec, read twice from Alpaca's own subscription tables**, once by the
account holder and once independently here before it was written down. The
*"Not verified"* item below — *whether the 30-symbol cap is per-stream or
global* — is answered, and the pessimistic reading this spec chose was wrong
in the expensive direction.

Alpaca's **Trading API** subscription tables (the Broker API tables are a
different product and are not these):

| Row | Equities Basic | Equities Algo Trader Plus | Options Basic | Options Algo Trader Plus |
|---|---|---|---|---|
| Websocket subscriptions | **30 symbols** | **Unlimited** | **200 quotes** | **1000 quotes** |
| Historical API calls | 200 / min | 10,000 / min | 200 / min | 10,000 / min |
| Real-time market coverage | IEX | All US Stock Exchanges | Indicative Pricing Feed | OPRA Feed |
| Historical data limitation | latest 15 minutes | no restriction | latest 15 minutes | no restriction |

**Two streams, two budgets, not one shared pool of thirty.** Every option
contract is still its own symbol; there are simply 200 slots for them rather
than 30, and the 30 equity slots are spent only on underlyings.

Three consequences, in descending order of how much they cost:

1. **`engine/stream.py` currently rations a resource that is not scarce, and
   the failure that produces is the exact one the module exists to prevent.**
   Its docstring reasons that *"grouped multi-leg reaches 32 option symbols at
   eight positions … before a single underlying is counted"*, which is true
   and no longer binding: 32 contracts fit inside 200 with 168 slots unused.
   Run against a 30-slot pool, a full eight-position book returns `dropped`
   units and surfaces *"N symbols not streamed"* for **position contracts that
   had ~170 free slots available**, leaving those positions marking at a stale
   price. A stale price looks exactly like a quiet market — the module's own
   words — so this is a silent, directional wrong number arriving through a
   wrong budget instead of through a silent server-side truncation. The
   module's logic is right; its inputs are wrong.
2. **"Unlimited" is true of the equity stream and false of the option
   stream.** Algo Trader Plus raises options to **1000 quotes**, a real
   ceiling. `runtime.UNLIMITED_STREAM_SYMBOL_CAP = 10_000` is a sound sentinel
   for equities on the paid plan and is **wrong for options on it** — it would
   let the engine subscribe past 1000 and be truncated or refused by the
   server, which is precisely the silent truncation `stream.py` was written
   against.
3. **The Markets page has spare equity slots.** `max_concurrent_positions` is
   8, so worst case eight distinct underlyings, leaving **~22 of the 30 equity
   slots** free. Those are free real-time marks at zero REST cost. Decision 18
   spends them.

**Connection limits are a separate axis, and the Trading API tables do not
carry one.** Checked deliberately, because the Broker API table has a *Stream
Connection Limit* column and it would be easy to read it across: the Trading
API Equities and Options tables have **no such row**. What the streaming docs
state instead is *"the number of connections to a single endpoint from a user
is limited based on the user's subscription, but in many subscriptions (or
without one) this limit is 1"*, with a `406 connection limit exceeded` on a
second one. **Per endpoint.** The stock stream, the option stream and
`trade_updates` are three different endpoints, so one connection each is
allowed and the three-socket design stands — and decision 1's *"Basic allows
one websocket per account"* is imprecise in a way worth correcting rather than
wrong in a way that changes anything: one *per endpoint*, which is still an
argument for one process, since two processes would contend for the same
endpoint and take the 406.

### The series ceiling counts served points, not fetched bars — verified 2026-09-14

**Probed, against a recorded fixture.** Two facts about the `period` /
`timeframe` pair on `GET /api/markets/stocks`, both of which arrived with the
regular-trading-hours filter committed on 2026-09-14 and neither of which is
described anywhere else in this spec. They belong under Constraints because
they spend the same 200/min `data.` bucket decision 18 already spends 150/min
of, and because one of them changes what a range control may offer.

**The fetch is about 2.46× the serve, so the size estimate bounds the
response and not the vendor traffic behind it.** `_in_regular_session` runs on
the way *out* of `_fetch_intraday`, while the request going *in* still asks
for the ~04:00–20:00 ET the bars feed returns whether asked or not: 960
minutes fetched per session against the 390 `REGULAR_SESSION_MINUTES` counts.
Twenty-six symbols over one `1Min` session now passes at 390 × 26 = 10,140
counted while fetching 960 × 26 = 24,960 bars — three pages at the provider's
`_BARS_PAGE_LIMIT = 10_000` — and the worst case `MAX_RESPONSE_POINTS =
20_000` permits is ten symbols over five `1Min` sessions: ~49,000 bars, about
**five** pages. Well inside `_MAX_PAGES = 50`, so nothing raises.

`MAX_RESPONSE_POINTS` was deliberately **not** scaled down by 2.46× to restore
the old two-page property. The ratio is a property of the *intraday* path
alone — `1D` is one bar per session on both sides of the filter — so scaling
the ceiling by it would refuse daily windows that cost the vendor nothing
extra, which is the default path and the common one. What is gone is the
*rationale*, not the number: the constant no longer means "two round trips",
it means "roughly 2 MB on the wire", and the round trips are now bounded by
`_MAX_PAGES` somewhere else entirely. Five pages is tolerable against 200/min
because this path runs when a human expands a chart rather than on the poll —
but it is also the one series path with **no cache behind it**, so a held
refresh key is five pages every time. Decision 20 is where this stops
mattering.

**`1H` cannot represent the session open.** Alpaca's hourly bars are aligned
to the *Eastern hour* and stamped at the interval's left edge, so the bar
covering 09:30–10:00 is stamped **09:00** and the half-open regular-hours
test (`open <= at < close`) drops it. An hourly session therefore serves
**six** points beginning at 10:00 ET rather than seven, and three on a
half-day. Verified against `tests/fixtures/alpaca/stock_bars_hourly.json`:
NVDA's stamps run `2025-11-26T14:00:00Z` (09:00 ET, dropped) then
`T15:00:00Z` (10:00 ET, the first kept), and the 2025-11-28 half-day carries
exactly three in-session stamps.

There is no exact answer available — the bar straddles the boundary, so
keeping it imports thirty minutes of pre-market into a figure labelled *"the
move over the window"* and dropping it loses the opening thirty minutes — and
dropping is the choice, because the filter's whole purpose is that no
extended-hours print reaches the series. So `1H` stays **askable** on an
explicit request and `_finest_timeframe_to_suggest` stops **offering** it: a
range control that offers a timeframe which cannot show the open is a control
that lies about the open. What a refusal suggests instead is the next
resolution that *can* begin a bar at the open — the refusal for a year at
`5Min` now names `1D`, which is the honest answer anyway, since a year of
intraday bars is not a thing a ~900px chart can draw. The estimate keeps the seventh bar anyway, since an
over-estimate is the safe direction for a ceiling.

### Not verified

Carried forward as implementation-time checks rather than assumptions:

- ~~**Whether the 30-symbol websocket cap is per-stream or global.**~~
  **Resolved 2026-09-14** — per stream, and the two budgets differ. See *The
  websocket cap is two budgets, not one* above and decision 17. The
  pessimistic design was wrong in the direction that drops position marks.
- **Whether option `trades` and `bars` subscriptions count against the same
  200.** The table's unit is *"200 quotes"* and the equity row's unit is *"30
  symbols"*, which is a difference in wording that may or may not be a
  difference in metering. Corollary subscribes option **quotes** for marking,
  so the pessimistic reading costs nothing today; it becomes a question the
  moment anything wants option trades. An over-subscription is documented to
  answer `405` with *"the symbol subscription request you sent would put you
  over the limit set by your subscription package"* — check that against the
  real socket rather than counting locally, and treat a 405 as authoritative
  over any constant in this repo.
- **Whether an equity symbol subscribed to two channels spends one slot or
  two.** Same shape as above, and same mitigation: the server's `405` is the
  source of truth, not `STREAM_SYMBOL_CAP`.
- **Whether `/v2/stocks/snapshots` caps the `symbols` list.** The reference
  documents *"a comma-separated list of stock symbols"* with **no stated
  maximum and no pagination**, and `AlpacaProvider.stock_snapshots` issues
  exactly one request for any symbol count. Decision 18's 400ms floor rests on
  that one-request-per-cycle fact. If a cap exists undocumented and the
  provider has to chunk, the per-cycle count rises and **the floor rises with
  it** — measure it against the real Markets universe before the interval is
  hard-coded, and derive the interval from the measured per-cycle count rather
  than restating 400.
- Market cap and average daily volume are absent from Alpaca (confident, unverified this session). `avgVolume` is computable from daily bars; market cap comes from Finnhub `/stock/profile2` → `marketCapitalization`.
- The stock snapshots endpoint's exact field shape.
- Whether `order_class: oco` is accepted for *options* — carried over unresolved from the Open Positions spec. Phase 6's problem, not this one.
- `non_marginable_buying_power` semantics on a cash account.
- Whether the same `id` really appears on both rows of an option-event pair, which would make `fill(activity_id UNIQUE)` drop half of every event. Check against the first real one.
- Whether `open_interest` is null because of the plan or because Alpaca populates it only after a settlement cycle this account has never had.
- Decision 20 carries **its own `Not verified` list**, for the claims that only
  become checkable once the subscription is bought: vendor IV/greeks coverage
  on OPRA, the option stream's metering unit at 1000, the inbound message rate
  at full subscription, and what the first invoice actually charges.

**Resolved since the original list:** the MCP 401 is diagnosed — the server holds non-paper keys, and the `.env` keys work — so the provider is no longer being built blind. The account, contracts, snapshot, portfolio-history and activities shapes above are probed.

---

## Open questions

Added by the 2026-09-10 amendment. These are the places where the probe found
an absence rather than a mismatch, so no amount of care in the mapper resolves
them. Each blocks a specific shipped behaviour, and each is a judgement about
what the terminal should say when it does not know something.

### 1. IV, greeks and open interest have no source on Basic

**RESOLVED 2026-09-10 — see decision 10.** The OPRA agreement was checked and
is paywalled, not a free click: it comes with Algo Trader Plus at $99/mo. The
decision is to derive IV and greeks locally, report open interest as
unavailable, and buy the plan as a Phase 6 prerequisite. The framing below is
kept because it is the reasoning decision 10 rests on.

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

**Largely resolved 2026-09-10.** Seven orders were placed on the paper account
— four `mleg` verticals and three single-leg — and one long was closed for a
round trip. That supplies real fills, a real `mleg` order with real
`ratio_qty` values for the grouper, eleven position rows across seven logical
positions, and one realized trade (−$7.00) for the matcher. Three of the
corrections above came out of it, including the two-hop join that decision 5
depends on.

**What it does not yet supply is the option-event path.** Every position
opened expires in November 2026 or later *(true on 2026-09-10 and false by the
next afternoon — see the resolution below)*, so `OPEXP`, `OPEXC` and `OPASN`
remain verified only against Alpaca's documented examples — and that is
exactly where the spec is most likely to be wrong, because the money sits on a
different row than the event. The open item is now narrow: hold something to
expiry. A near-dated contract would answer it within days rather than months.

**Resolved 2026-09-11 — the data already exists; no further orders needed.**
Buying near-dated contracts was approved, and then a read of the live account
found the test already running. Three single-leg NVDA positions were opened
2026-09-10 13:37 ET expiring **2026-09-11**, and with NVDA closing at 218.17
they cover all three branches at once:

| Contract | Position | Close vs strike | Event |
|---|---|---|---|
| `NVDA260911C00205000` | long | ITM by $13.17 | auto-exercise → `OPEXC` pair |
| `NVDA260911C00240000` | long | OTM | worthless → `OPEXP` |
| `NVDA260911P00230000` | **short** | ITM | assignment → `OPASN` |

`OPASN` was written off above as the branch that would be validated by
accident or not at all. It is covered, because a *short* near-dated put was
among them. All three settle overnight, so the activity rows land 2026-09-14
at the latest. **Record them as fixtures before anything else in step 5** —
this is the one window where they exist and nobody has to imagine them.

Two facts the same read established, both of which the spec asserts elsewhere
and now has evidence for: `/v2/positions` returns **no multiplier field at
all** (`multiplier: None` on every row), so per-contract multipliers genuinely
must come from the contracts endpoint; and the account reports
`multiplier: 4`, `options_approved_level: 3`, `options_buying_power: 69886.08`
against `cash: 96886.08`.

Consequence to expect on 2026-09-14: the exercise and the assignment both
deliver **stock**. The account will open holding roughly 200 NVDA shares —
100 bought at 205 through the call, 100 at 230 through the assigned put. That
is the case the matcher handles by *naming* the shares rather than tracking
them, so it is also a live test of that boundary rather than a hypothetical.

The reason this is worth days of waiting rather than a fixture: the failure
mode is **silent and directional**. Nothing raises, because `net_amount: 0` is
a well-formed number. Every ITM expiry books as a total loss, and lifetime
P&L, average win, average loss and win rate are then all wrong in the same
direction with nothing on screen to say so. The first symptom is a terminal
that believes you never win.

**CLOSED 2026-09-12 — the events posted early, and the ledger reconciles to
the broker exactly.** All three rows landed on 2026-09-11, two days ahead of
the expected 09-14, and every prediction above held:

| Event | Symbol | `qty` | `net_amount` | `group_id` pairing |
|---|---|---|---|---|
| `OPEXP` | `NVDA260911C00240000` | `-1` | `0` | alone — nothing was delivered |
| `OPEXC` | `NVDA260911C00205000` | `-1` | `0` | paired with `OPTRD` 100 NVDA @ 205 |
| `OPASN` | `NVDA260911P00230000` | **`+1`** | `0` | paired with `OPTRD` 100 NVDA @ 230 |

The sign convention is confirmed live: `-1` when contracts leave a long,
`+1` when a short is assigned away. `group_id` is confirmed as the only
linkage — the `OPEXP` sits in a group by itself, which is exactly right,
because an OTM expiry delivers nothing and so has no `OPTRD` to pair with.

One ingestion pass over the real account: **40 activities pulled, 18 fills,
4 realized trades, 4 mleg groups, 0 refusals.** The matcher does *not* trust
`net_amount`:

| Contract | Booked close | Implied NVDA | P&L | A total-loss matcher would book |
|---|---|---|---|---|
| $205 call, exercised | `13.29` | 205 + 13.29 = **218.29** | −$91 | −$1,420 |
| $230 put, assigned | `11.71` | 230 − 11.71 = **218.29** | −$16 | −$1,155 |
| $240 call, expired | `0` | — | −$2 | −$2 ✓ correct |

Two independent contracts agree on 218.29 to the cent, which is NVDA's
official SIP close for 2026-09-11 — so the settlement price is sourced once
per `(underlying, session)` and shared, as `_ensure_settlements` documents.

**The reconciliation that closes this question.** The ledger exists because
Alpaca does not expose realized P&L, which also means Alpaca cannot confirm
it — so *"validated against what?"* was the real open item, not *"is there
data?"*. There is exactly one identity that ties a computed figure to a
number the broker states independently, and it holds to the cent:

```
broker equity  99,901.08  −  deposits 100,000  =  −98.92

  realized (ledger)     −116.00
  unrealized (open)      +18.00
  fees                    −0.92
                        ────────
                         −98.92
```

The fee leg is five distinct types, not one: 15×`OCC` (−0.45), `ORF`
(−0.23), `REG` (−0.21), `TAF` (−0.02), `CAT` (−0.01). A matcher booking
every exercise as a total loss — the failure this question was written about
— would show here as a gap near **−$2,500**. The gap was fees.

**Do not treat this as permanent proof.** It is one account, one session,
four closed trades, and no adjusted contract has ever been held here, so
open question 4's refusal path is still untested against real data. What the
identity does establish is that the FIFO matcher, the mleg grouper, all
three option-event branches, the intrinsic settlement path and fee handling
are not *collectively* wrong — which is a much stronger statement than any
one of their unit tests makes, and it should be re-run whenever the ledger
changes. It is cheap: equity, deposits, realized, unrealized, fees.

What follows is the original framing, kept because the reasoning still applies
to the part that is open.

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

### 4. An exercised adjusted contract has no bookable P&L — RESOLVED: refuse

**Opened 2026-09-11**, during step 5's audit. The matcher shipped with a real
bug: it correctly refused to state a *share count* for an adjusted contract —
a `GME1` does not deliver 100 shares — and then used that same multiplier to
state a *dollar P&L*, booking a phantom **+$1,200 realized gain** with no
rejection anywhere. Reproduced before it was fixed.

The fix is a refusal: an adjusted root reaching an `OPEXC` or `OPASN` branch is
rejected and logged rather than booked. Deriving the ratio from `deliverables`,
`size`, or an assumed split factor was **explicitly forbidden**, because each
is a guess about money and rule 4 exists to stop exactly that.

What has no answer yet is what the *correct* number is. Refusing means those
trades never reach realized P&L at all — a gap in lifetime figures rather than
a wrong value in them, which is the right trade tonight and not obviously the
right one forever. The scanner already filters adjusted contracts out of the
universe, so Corollary will never *open* one; this only bites on a contract
adjusted **while held**, which is rare and exactly when the numbers matter
most.

**Resolved 2026-09-12: refuse and report, permanently enough to build on.** The
deliverable data was examined before deciding. A real `GME1` contract delivers
**100 GME shares plus 10 GME.WS warrants**, split `allocation_percentage`
95/5, while `multiplier` and `size` both report `100`:

```
GME1261016C00003000   root: GME1   underlying: GME   multiplier: 100   size: 100
  { symbol: "GME",    amount: "100", allocation_percentage: "95", type: "equity" }
  { symbol: "GME.WS", amount: "10",  allocation_percentage: "5",  type: "equity" }
```

Computing from that was rejected for five reasons, in order of weight:

1. **It stops being a display question at Phase 6.** `max_daily_loss_pct` is
   enforced against realized P&L, so a phantom gain — the exact +$1,200 bug
   found here — offsets real losses and **suppresses a halt that should have
   fired**. A missing trade makes the halt fire early, which is the safe
   direction. This is rule 4 failing open versus failing closed.
2. **Nothing validates the result.** The ledger exists because Alpaca
   publishes no P&L, so bad deliverable arithmetic has no second source to
   catch it. Silent, inside a figure labelled lifetime P&L.
3. **The warrant leg has no dependable mark**, least of all a historical one
   at exercise time. Valuing it also drags equity-and-warrant pricing into a
   phase that deliberately excludes it — the matcher *names* delivered shares
   rather than tracking them.
4. **`allocation_percentage` is an accounting convention, not a P&L
   instruction.** Nothing in the API says what it is for, and treating 95/5 as
   a strike split is a guess wearing a number's clothing.
5. **It cannot be tested.** No adjusted contract has ever been held here, and
   waiting for a corporate action on a live position could take years — the
   same untestability that made the option-event path a risk, minus the luck
   that resolved it.

Refusing keeps the raw `fill` and activity rows either way, so the decision is
reversible in the direction that matters: history can be recomputed once there
is one real case to model against. Booking a wrong number is only reversible
once somebody notices, which is the failure this refuses to risk.

The Activity page states the gap rather than hiding it — *"N trades not
booked — adjusted deliverable"* beside the header cards, in §8.5's words. A
known-incomplete figure, never a quietly wrong one.

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

Twenty decisions, taken 2026-09-09 through 2026-09-14. Each records what it rules out, because the alternative is usually the thing someone reaches for later.

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

### 10. Derive IV and greeks where Alpaca does not; buy the plan at Phase 6

**Amended 2026-09-10, second probe.** The first probe sampled two contracts
and generalised from them. Both of its conclusions were wrong, and the error
has one shape: `/v2/options/contracts` and the snapshots endpoint both return
contracts ordered by strike, so a small `limit` returns the deep-in-the-money
tail — the least liquid, most recently listed end of the chain — and nothing
about it generalises to the book.

**`impliedVolatility` and `greeks` ARE served on `indicative`,** for the
contracts where Alpaca's own solve succeeds: 19 of 100 on NVDA, 12 of 100 on
AAPL, concentrated near the money and on established expiries. Not absent —
*partial*. The provider therefore passes vendor analytics through where they
exist and derives only where they do not, and must record which of the two a
number came from, because a chain silently mixing measured and derived values
is worse than either alone.

**`open_interest` IS populated,** on 98 of 100 NVDA contracts and 79 of 100
AAPL, with `open_interest_date` and `close_price` alongside. The first probe's
nulls were newly-listed contracts that genuinely had no settled interest yet —
which the original "Not verified" list had offered as a hypothesis and the
first amendment wrongly dismissed. **PRD §8.4's "highest open interest" screen
has a ranking key and should be built.** A null still means absent and must
survive as null, but it is now the exception rather than the column.

What survives unchanged is the timing, and it is worth restating because the
reasons it rests on have narrowed: what the subscription buys is **real-time
quotes instead of 15-minute-delayed, and unlimited stream symbols instead of
thirty.** Neither matters while the terminal is read-only; both matter the
moment it executes, because an order priced off a 15-minute-old quote is a
loss mechanism where a stale column is only a stale column. The original
decision was right for reasons that were partly wrong.



Taken 2026-09-10, after the OPRA agreement was confirmed paywalled rather than
a free signature.

**IV and greeks are computed locally** with Black-Scholes, from the mid of the
indicative quote. This is not a workaround for a missing measurement: Alpaca's
own OpenAPI document describes its `impliedVolatility` and `greeks` as
*"calculated using the Black-Scholes model"*, so the vendor derives them too
and paying $99/mo buys the same arithmetic run on their hardware. The risk-free
rate comes from FRED, already a planned §7 dependency, and the dividend
assumption is stated rather than hidden.

**Open interest is reported absent**, in §8.5's fixture-marker words. It is a
fact about the market and cannot be derived from a price at any effort. PRD
§8.4's "highest open interest" screen has no ranking key on this plan and must
say so rather than rank on nulls.

**The plan is bought before Phase 6, not before Phase 2.** What the
subscription actually buys is three things: open interest, real-time quotes
instead of 15-minute-delayed, and unlimited stream symbols instead of thirty.
None of the three changes anything while the terminal is read-only. All three
change something the moment it executes — and the middle one is the reason the
timing is a rule rather than a preference: **an order priced off a
15-minute-old options quote is a loss mechanism, not a display nicety.** The
same staleness that is acceptable in a column is unacceptable in a fill.

The honest cost of deriving: greeks computed from a delayed mid are *delayed*
greeks. Correct method, stale inputs. That is fine for a column and not fine
for sizing, which is the same boundary stated above from the other side.

Rejected: buying now, which spends roughly $400 across phases 2–5 to change no
behaviour in any of them. Also rejected: dropping the IV column, which
discards shipped Phase 1 work to avoid arithmetic the vendor has already
told us how to do. Also rejected: ranking the open-interest screen on nulls,
which is the invented-number failure §8.5 exists to prevent.

**Consequence for `engine/stream.py`:** the 30-symbol budget manager stays,
and stays necessary. It was designed for the pessimistic reading of the cap
and is the component the subscription would make redundant — but it is also
the graceful-degradation path, so it survives the upgrade rather than being
deleted by it.

### 15. Open interest has a free source, and we may not use it -- 2026-09-12

Decision 10 said open interest is unavailable on Basic and left it there. That
was right about Alpaca and incomplete as a statement about the world, so the
question got asked again. Recording the answer so it does not get asked a
third time.

**CBOE publishes it, free and unauthenticated.** The endpoint its own quote
pages read returns 4,026 NVDA contracts, 2,988 of them with non-zero
`open_interest`, and carries `iv`, `delta`, `gamma`, `theta`, `vega`, `rho`
and a theoretical price besides -- everything decision 10 writes off. The
underlying close it reported (218.29) matches the official SIP close.

**We may not build on it.** CBOE's delayed-quote pages state that it is
*"strictly prohibited to download delayed quote table data ... by using
auto-extraction programs/queries and/or software"*, that they *"will block IP
addresses of all parties who attempt to do so"*, and that downloading *"in any
other way than by manual ticker symbol entry is strictly prohibited"*. The
data is CBOE LiveVol's property. A scheduled fetch is precisely the prohibited
case. The sanctioned route is their commercial All Access API, which is itself
the answer to why this is not free.

So decision 10 stands, with its wording corrected: open interest has **no
permissible free source we have found**, which is a different claim from
"unavailable". Two documented APIs with terms that do permit programmatic use
-- Alpha Vantage's `HISTORICAL_OPTIONS` and Tradier's sandbox -- are untested
and were deliberately deferred: Phase 6 buys Algo Trader Plus anyway, and that
supplies open interest, IV and greeks from the vendor already in the order
path rather than adding a provider to maintain for one column.

Consequence for the Markets page: the OI **column** stays and renders
unavailable, per decision 10. The named **sort screen** *"Highest open
interest"* is **removed**. A ranking option that returns the list unsorted is
worse than an absent one -- the same reason the command palette omits Execute
while the engine is halted rather than showing it disabled. It comes back when
the data does.

### 16. A Phase 2 surface with no source states why, from real state -- 2026-09-12

The Dashboard's *Recommended Trades* panel has no source in this phase: the
scanner and the LLM layer are both explicitly out of scope, so nothing can
generate a candidate.

It renders an **empty state driven by `/api/engine/state`** rather than
fixtures or a hidden panel. The engine seeds halted on cold start (rule 9), so
"the engine is halted, no candidates are being generated" is a true sentence
assembled from live data, not a placeholder -- and the panel becomes real for
free when Phase 4 arrives, with no layout to rebuild.

Rejected: keeping the mock recommendations behind a fixture badge, because
PRD §8.5 is explicit that a table of invented numbers reads as invented, and
this one carries strike, expiry and a confidence score. Also rejected: hiding
the panel, which changes the layout now and changes it back later.

### 11. The Activity page folds every trade, and pages the table

Taken 2026-09-11. `Money` raises on `SUM`, `AVG`, `MIN`, `MAX` and `ORDER BY`,
so lifetime realized P&L, average win, average loss and win rate are Python
folds over loaded `realized_trade` rows rather than SQL aggregates. The
decision is **which rows to load**, and it is: all of them for the header
cards, with pagination on the table itself.

A single-user account will not approach a painful row count for years, and the
alternative failure is worse than a slow query. A capped window makes four
figures labelled "lifetime" mean something narrower than the word, and a
precomputed rolling aggregate introduces state that can drift from the trades
it summarises — drift in a P&L figure being exactly the kind that goes
unnoticed. If the fold ever does get expensive, the fix is a cached aggregate
*derived on write from the same rows*, so it can be rebuilt and checked
against them; that is a different thing from a counter that is only ever
incremented.

Rejected: a trailing-12-month window with the period named in the card
(honest, but discards the truest number on the page), and maintained running
totals (fastest, unreconcilable).

### 13. The ingestion cursor is never `MAX(activity_id)`

Taken 2026-09-12, after reading the recorded ids rather than reasoning about
them. The shape is uniform — a 17-character prefix, `::`, a UUID — which is
exactly what makes the trap convincing:

```
FILL   20260910131125217::a9d576c2-…     real time, 13:11:25.217
FEE    20260910000000000::a2a0c406-…     zeroed
JNLC   20260805000000000::4b47d1d0-…     zeroed
```

**Non-trade rows carry a date-only id with the time zeroed out.** Within any
one day, every `FEE`, every journal — **and every `OPEXP`, `OPEXC` and
`OPASN`** — therefore sorts *before* every fill of that day.

Ingestion is idempotent on `activity_id` and resumes from the newest row it
holds. Take that cursor as `MAX(activity_id)` and it lands on the day's last
**fill**, with that same day's expiry and assignment rows sitting *below* it.
The next pull asks for everything newer and **never sees them again** — not
late, not duplicated, gone. Missing rows become missing realized trades become
a wrong lifetime P&L, with nothing anywhere saying so.

So the cursor is **`since_id`, the vendor's own page token** — the id of the
last activity in the page just pulled, not a `MAX` over anything. Alpaca
defines its own pagination order and that token is authoritative within it,
which is stronger than any column this end could sort on. Step 7's ingestion
reached this independently, from the same recording, before the decision was
written down; what follows is the guard, not the fix.

Rejected on the way: `transaction_time` as the ordering column, and an
explicit integer ingest sequence. The first fails because non-trade rows carry
`date` rather than `transaction_time` — the column that would do the ordering
is unpopulated on exactly the rows most likely to be skipped. The second works
but invents local state to replace a token the vendor already supplies
correctly.

`activity_id` additionally takes the same SQL guard `Money` already carries:
ordering, comparison and aggregation **raise** rather than answering. **No
carve-out for `MAX`** — an earlier draft proposed one on the grounds that it
is a legitimate resume cursor, and it is precisely the opposite. The guard
costs a few lines of Python where one line of SQL would have fitted, and buys
a resume point that cannot silently skip.

Rejected: sorting on `transaction_time` in SQL instead. Non-trade rows carry
`date` rather than `transaction_time`, so the column that would do the
ordering is not populated on the rows most likely to be skipped.

### 14. A refused closing carries its reason, persisted — decided 2026-09-12

`ActivityStats` ships `notBooked` and `notBookedSymbols`: how many closings
the lifetime figures are missing, and which contracts they were. That much is
arithmetic over two tables and needs nothing new.

What it deliberately does **not** carry is *why* each gap exists. The
`RejectionRule` is logged by `engine/ledger.py` and no Phase 2 table stores
it, so an unverified-deliverable refusal is the case the count exists for
rather than provably the only thing that can produce one. The route was
written to state the count and stop, rather than to assert a cause it cannot
evidence — correctly, because a confident wrong reason on a money figure is
worse than an admitted gap.

**Decided: persist the refusals, at step 8.** Either a `ledger_rejection`
table, or `IngestResult` handed to the API — step 8 is already the point
where `EngineRuntime` owns the ingest loop and its result, so the second is
likely cheaper. The Activity page then reads:

```
⚠ 1 trade not booked — adjusted deliverable
   GME1261016C00003000
```

rather than a bare count the reader has to reconcile by hand against the
engine log or the broker's own history.

Why it is worth a table rather than left to the log. Rule 8 already requires
every rejection to record its rule, inputs and timestamp, so the information
exists — the question is only whether the person looking at a wrong lifetime
P&L can see it. A gap with a stated cause is a decision the reader can agree
with; a gap without one is indistinguishable from a bug, and the reader's
only honest response is to stop trusting the number. That is the same
standard §8.5 sets when it says a fluent non-answer is worse than an admitted
gap.

Not urgent, and deliberately sequenced after step 7: `notBooked` is **0** on
the live account today, because no adjusted contract has ever been held here.
Nothing is being hidden while this waits. It becomes load-bearing the moment
one is, which is also exactly when nobody will be in a position to reconstruct
the reason by hand.

### 12. Finnhub stays, reaffirmed 2026-09-11

Decision 7 was re-examined against the alternatives and stands. The market-cap
column was never the real question: PRD §7 already assigns Finnhub the news
feed, the economic and earnings calendar, and analyst consensus via
`recommendation-trends`, so dropping it for step 9 defers a dependency rather
than removing one.

What the survey found, recorded so it is not re-run: `yfinance` is far and away
the most popular financial-data library in the Python ecosystem (~25k GitHub
stars against `finnhub-python`'s ~870), and is nonetheless the wrong
dependency here — an unofficial Yahoo scraper with no SLA and a record of
breaking when Yahoo moves. Among official keyed APIs with a usable free tier,
Finnhub is both the most popular and the most generous (60 req/min). FMP ships
no official client; Alpha Vantage's free tier has narrowed to the point of
being unusable and is already benched in PRD §7 as the documented consensus
fallback; Polygon has rebranded to Massive and moved real-time behind $199/mo.

The one genuinely vendor-free alternative, kept on the record because it may
matter later: market cap is shares outstanding × price, Alpaca already
supplies the price, and SEC EDGAR's XBRL `companyfacts` API supplies shares
outstanding for free with no API key. Its costs are a CIK mapping, tag
selection across the `dei` and `us-gaap` taxonomies, a declared User-Agent,
and a share count stale by up to a filing quarter — immaterial for a column
used to rank and bucket, since price moves dominate. Its coverage gap is
funds, which file no share count, and that gap lands exactly on the `null`
`markets.ts` already renders deliberately for a fund's `marketCap`. If Finnhub
is ever dropped project-wide, this is the replacement for this column, and
that decision belongs at the PRD §7 level rather than at step 9.

### 17. Two stream budgets, planned one stream at a time — 2026-09-14

The cap correction above is a fact; this is what the engine does about it.

**`plan_subscriptions` is called twice — once per stream — and its allocation
logic does not change.** Option units are fitted against 200, equity units
against 30. The function already takes `cap` as a parameter and already
refuses to split a unit, dedupes for free, cuts as a strict prefix and reports
`dropped`, `not_streamed` and `spare_capacity`. All of that is correct at any
cap. What was wrong was the number it was handed and the arithmetic in its
docstring, and both are inputs.

So the change is **constants, inputs and one new guard** — not logic:

- `STREAM_SYMBOL_CAP = 30` becomes two named constants with the stream in the
  name: an equity symbol cap of 30 and an option quote cap of 200. The single
  unqualified name is the bug's habitat and should not survive the fix.
- `runtime.stream_symbol_cap_for_plan(plan)` becomes per-stream. On Algo
  Trader Plus the equity cap is the existing `UNLIMITED_STREAM_SYMBOL_CAP`
  sentinel and **the option cap is 1000, a real ceiling** — the paid plan does
  not make options unlimited, and using the 10,000 sentinel there would
  subscribe past a limit the server enforces.
- `EngineRuntime.plan_stream_subscriptions` returns **two plans**, and its
  caller subscribes each to its own socket.
- The UI's *"N symbols not streamed"* sums across both plans. One number, not
  two banners: the reader's question is *"is anything I hold unmarked"*, and
  which socket ran out is a detail for the log record, which already carries
  `cap` per drop.

**The one genuine logic addition is a refusal.** A `SubscriptionUnit` is
all-or-nothing over its symbols, and that promise cannot survive a unit whose
symbols land in two different budgets — half of it would be admitted and half
dropped, which is the three-legs-live-one-leg-stale failure the all-or-nothing
rule exists to prevent. So a unit carrying both an OCC symbol and an equity
ticker is **rejected as a caller bug**, alongside the existing empty-unit and
empty-symbol refusals. `contract_unit` and `underlying_unit` already satisfy
this by construction; `recommendation_unit` does not, and Phase 4 must build a
recommendation as one option unit plus one equity unit rather than one mixed
unit. That mirrors what positions already do, where contracts and underlyings
are separate units at separate priorities.

**Blast radius, stated so nobody re-derives it.** `stream.py` is pure, has no
vendor import, reads no clock and no environment, and is covered by
`tests/engine/test_stream.py`. Every test that spells `STREAM_SYMBOL_CAP`
keeps working against the renamed equity constant; the new work is a second
set at 200 and one test proving a mixed unit raises. `runtime.py` owns the
plan lookup and is where the per-stream split lands. Nothing else in `engine/`
or `api/` reads the cap.

Rejected: **teaching `plan_subscriptions` two budgets internally**, by giving
it a cap per asset class and classifying symbols itself. It is the obvious
move and it puts OCC-symbol parsing — a vendor-shaped concern — inside the one
module that is deliberately ignorant of vendors, and it turns a prefix cut
with one `remaining` into two interleaved cuts whose drop ordering has to be
reasoned about again. Two calls reuse a proven function.

Rejected: **leaving the cap at 30 because it is the safe direction.** It is
not. A cap below the real one does not degrade gracefully here — it drops
position contracts and reports a stale mark as a live one, which is the
failure mode the module was written against. Under-spending a budget is only
the safe direction when the thing being rationed is optional, and a held
contract's mark is not.

Rejected: **reading both caps from the environment**, next to the three feed
names. Feed names are genuinely per-deployment; these are facts about a
published price list, and the plan of record already lives in
`ALPACA_DATA_PLAN`. A cap in the environment is a cap that can be raised by
someone who has not paid, and the failure is silent truncation.

### 18. Adaptive cadence: push where it matters, 400ms/5s where it does not — 2026-09-14

**What was asked for and what is being built.** The account holder asked for
~150–200ms in the foreground, ~5s in the background, positions *"as close to
live as possible"*, and said account and activity may lag. 150ms is 400
req/min and 200ms is 300 req/min against a hard 200/min; 300ms is exactly at
the ceiling with nothing left for a chain. So the interval was not adopted as
asked. What is built is **faster than what was asked for on the two surfaces
that were the point of asking**, and the substitution was stated plainly
rather than quietly rounded:

| Surface | Cadence | Why |
|---|---|---|
| Position contracts and their underlyings | **push, no interval** | A websocket has no cadence to tune, and it beats 150ms. All 32 worst-case contracts fit the 200-quote budget (decision 17), so nothing is dropped. |
| Markets, visible rows | **push** | The ~22 equity slots left after position underlyings. Zero REST cost, and Alpaca's own docs recommend the stream over polling the latest endpoints. |
| Markets, every row | **400ms foreground, 5s background, stopped when hidden** | 150/min of a 200/min bucket, leaving ~49/min for on-demand chains. |
| Account, positions, activity | **15s**, unchanged | Explicitly allowed to lag, and on the other token bucket anyway. |

**Foreground, background and hidden are three states, not two.**

- **Hidden** — `document.hidden` is true. The poll **stops**, which is what
  `useMarketPoll` already does and it is kept. Not slowed to 5s: nothing is
  rendered, so a background tab at 5s is 12 requests a minute of work nobody
  can see, and the money-bearing numbers are on a server-side stream that does
  not care whether a tab is painted. On return the first poll is immediate,
  before the interval restarts — the existing behaviour, and the reason a
  revisit does not show two seconds of skeletons.
- **Foreground** — visible **and the Markets route is mounted**. 400ms. Which
  page is open is the new term, and it is read from the router rather than
  from a store flag: the component that consumes the data is the component
  that mounts the hook, so `useMarketPoll(FOREGROUND_MS)` in `Markets.tsx` and
  nothing to keep in sync. A flag set on navigation is a flag that survives a
  crash, a modal, or a route the author forgot.
- **Background** — visible, Markets not mounted. 5s, driven by one app-level
  hook so exactly one interval exists at a time. It exists so that arriving at
  Markets renders a five-second-old table instead of skeletons, and so the
  server's session-volume and daily-series caches stay warm; at 12/min it is
  noise against the budget.

The two states are one hook and one constant pair, not two hooks: `Markets.tsx`
mounting simply supersedes the app-level interval, and the app-level one
resumes on unmount.

**The server bounds the rate; the client only requests it.** This is the part
that is easy to leave out and is load-bearing. The browser drives the poll and
the API forwards it to Alpaca, so **two tabs, a reload loop, or a hot-reloading
dev server multiply the Alpaca rate by the number of clients** — 400ms × 2
clients is 300/min against a 200/min ceiling. `ratelimit.py`'s bucket *waits*
rather than refusing, so the symptom is not an error: it is every Markets
request getting slower until the page looks broken for a reason nothing logs.
So `GET /api/markets/stocks` gains a **coalescing cache keyed on the requested
symbol set, with a TTL equal to the foreground interval** — concurrent and
near-simultaneous callers share one in-flight Alpaca request, and the Alpaca
rate is bounded by wall-clock rather than by client count. The token bucket
stays as the hard backstop; the cache is what keeps it from ever being reached.

**One map, one price — how a streamed row and a polled row coexist.**
CLAUDE.md is explicit that a second quote map is how the Markets table and an
Activity row end up disagreeing about AAPL, and Phase 2 adds a second *writer*
to the one map, which is the same hazard one level down. Four rules:

1. **An entry carries its provenance.** `UnderlyingQuote` grows `at` — the
   **vendor's** observation timestamp, not the client's clock — and
   `source: 'stream' | 'poll'`. Without those, two writers have no way to tell
   a newer price from an older one and the last one to arrive wins, which on a
   400ms poll racing a push means the screen flickers backwards in time.
2. **Price is last-observation-wins on `at`, with the stream winning a tie.**
   Both feeds are the same IEX feed on Basic, so the two timestamps are
   comparable and the comparison is meaningful rather than a heuristic. A poll
   response older than the entry already held is **discarded for price and
   applied for everything else**, which is the next rule.
3. **The merge is field-level.** The stream carries price and nothing else;
   `previousClose`, session volume, average volume and market cap arrive only
   on the poll. A stream write must never blank them, and a poll write must
   never stomp a fresher streamed price. One writer per field, except price,
   where rule 2 decides.
4. **Everything derived is derived at read time.** `change` and `changePct`
   come from `price` and `previousClose` in a selector, never stored. Two
   writers storing a price and a change independently is how a row reports
   +1.2% beside a price that is down — the disagreement CLAUDE.md warns about,
   arriving inside a single row instead of between two pages.

`lastTickAt` and `lastPollAt` stay **separate** and neither becomes the other:
*"is the stream alive"* and *"is the poll alive"* are different questions, the
stale pill answers the first, and collapsing them would let a healthy poll
hide a dead socket.

**The viewport hint is a hint, and it can only ever occupy the lowest tier.**
The engine owns the socket, so the client has to say which Markets rows are on
screen: a message on the existing WS, debounced on a settled viewport and sent
only when the set actually differs. The server treats it as input to a **new
lowest** `SubscriptionPriority` and nothing else. A client-supplied list can
never outrank a position contract or an underlying — rule 4's principle
applied to a stream budget rather than to a risk limit, because a client that
could evict a held contract from the stream could make a position mark stale
by scrolling. Churn is harmless by construction: every Markets row is polled
anyway, so losing a slot costs freshness, never a price.

Rejected: **150ms or 200ms as asked.** Both exceed a hard server-side limit,
and the failure is latency creep rather than an error, so it would have looked
like it worked. Stated to the account holder rather than silently rounded.

Rejected: **300ms.** Exactly 200/min, zero headroom, and the first chain click
of the session pushes the process into the bucket's wait path.

Rejected: **a fixed 2s poll** (the previous design). It is well inside budget
and it wastes 85% of it on a page whose entire job is watching prices move.

Rejected: **client-side throttling alone**, without the server cache. A
single well-behaved tab is not the case that breaks the budget; a second one
is, and no amount of discipline in one browser tab constrains another.

Rejected: **excluding streamed symbols from the poll request.** Saves zero
requests — it is one request either way — and opens a gap the moment a
subscription is dropped.

Rejected: **a second quote map for polled rows**, or keying quotes per page.
Named only to rule it out, because it is the thing someone reaches for when
two writers first collide, and it is the exact failure CLAUDE.md spends a
paragraph on.

Rejected: **hidden-tab polling at 5s.** Twelve requests a minute for a
rendering nobody sees, and it weakens the one honest signal the stale pill
has.

### 19. Algo Trader Plus is bought at Phase 4, not Phase 6 — 2026-09-14

Decision 10 set the trigger at Phase 6, on the argument that what the
subscription buys — real-time instead of 15-minute-delayed quotes, OPRA
instead of indicative, unlimited equity stream symbols — *"changes nothing
while the terminal is read-only"* and changes everything the moment it
executes. The account holder has moved it to **Phase 4**.

The reasoning that moves it: Phase 4 is where the strategy runtime and the LLM
layer start **generating candidates**, and a candidate is a recommendation to
put money somewhere at a price. Decision 10's own boundary — delayed inputs
are *"fine for a column and not fine for sizing"* — is crossed by a scanner
that ranks and sizes, not by the order that eventually follows it. A candidate
priced off a 15-minute-old mid is wrong before a human ever sees it, and the
`unvalidated` badge on a Research recommendation does not say *"the price this
was built on is a quarter of an hour old"*. Buying it at Phase 4 also puts a
month or two of real-time data behind the first order rather than switching
feeds in the same phase that first submits one — a feed change and a first
`submit_order` in one phase is two variables in one experiment.

Cost of moving it: roughly **$99–200 more** than waiting, being the one or two
extra months across Phases 4 and 5. Decision 10 rejected buying at Phase 2 for
spending ~$400 to change no behaviour; this spends a fraction of that to
change behaviour in the phase that starts recommending trades.

**What the upgrade actually changes, so it is not rediscovered:**

- **Three environment variables, and nothing else in the code** —
  `ALPACA_OPTIONS_FEED=indicative→opra`,
  `ALPACA_STOCK_FEED_HISTORICAL=sip` (already `sip`, unchanged),
  `ALPACA_STOCK_FEED_REALTIME=iex→sip`. Read only inside
  `data/providers/alpaca.py`, per CLAUDE.md. Plus `ALPACA_DATA_PLAN=basic→algo_trader_plus`,
  which is the plan of record `runtime.data_plan` and `api/routes/settings.py`
  both read.
- **The equity stream cap disappears** (30 → Unlimited). The option stream cap
  rises to **1000 quotes** and does not disappear — decision 17.
- **REST goes to 10,000/min on both buckets**, which is 50× the current data
  budget.
- **The 15-minute historical embargo lifts**, which removes the constraint
  that forces `_fetch_session_volumes` to read today's volume from a
  historical-feed bar rather than a live one.
- **`impliedVolatility`, `greeks` and `open_interest` arrive from OPRA**
  rather than being partly derived, so decision 10's `iv_source` label stops
  reading `derived` for most of the chain. The label stays: it is what makes
  the mixed case legible, and the mix does not vanish, it shrinks.

**What that then permits, at Phase 4 rather than Phase 6:**

- The **whole Markets table on the stream**, not just the visible rows, and
  the poll demoted from the freshness floor to a **reconciliation net** — a
  slow sweep that catches a symbol the socket has gone quiet on. PRD §7
  already says the polled path stays the default so the app degrades
  gracefully; that survives, at a much lower rate.
- **150ms becomes affordable** if it is still wanted — 400 req/min against
  10,000. It will probably not be wanted, because by then the rows that
  matter are pushed.

**Which Phase 2 constraints become vestigial, flagged so they are not left
looking permanent.** Each of these is correct today and misleading after the
upgrade, and none should be deleted in Phase 2:

- The **equity** branch of `stream.py`'s budget — the option branch stays real
  at 1000. The module is also the graceful-degradation path, so it survives
  the upgrade rather than being deleted by it; decision 10 already said this
  and it still holds.
- Decision 18's **400ms floor and its coalescing cache**, which exist to
  ration a 200/min bucket.
- The Markets **viewport hint**, whose whole purpose is choosing which ~22
  rows get the leftover slots.
- The **IV/greeks derivation path** and `iv_source: 'derived'` as the common
  case.
- `LiveStatus`'s and `Markets.tsx`'s on-screen sentences explaining why the
  chain is not streamed.

Rejected: **Phase 6, as decision 10 had it.** It draws the line at the first
real fill, which is the right line for *execution* risk and the wrong one for
*origination* risk — a bad candidate generated at Phase 4 is a bad order at
Phase 6, arriving with a confidence score attached.

Rejected: **Phase 2 or Phase 3.** Decision 10's arithmetic is unchanged: the
read-only terminal learns nothing truer from a real-time quote than from a
15-minute-old one, and a stale column is a stale column.

Rejected: **buying it only for the phase that needs it and cancelling.** A
monthly subscription toggled per phase makes the feed configuration a moving
target across a period when the scanner's own determinism is being
established, and the saving is one month.

### 20. The post-upgrade cadence: stream the universe, slow the poll, keep the option budget real — 2026-09-14

Decision 19 moved the purchase to Phase 4 and listed what the upgrade
changes. This is what the **cadence** becomes on the other side of it, written
now so that upgrade day is four environment variables, two constants and a
short list of deletions rather than a redesign taken under the pressure of a
subscription already being billed. Nothing here is Phase 2 work; every item is
marked Phase 4 in *Order of work*.

**Re-verified independently today** against
`https://docs.alpaca.markets/us/docs/about-market-data-api`, because a cadence
designed against a misremembered limit is the failure of decisions 17 and 18
repeated. The Trading API subscription tables read exactly as decision 17
records them — equities 30 symbols to **Unlimited**, options 200 quotes to
**1000 quotes**, historical API calls 200/min to **10,000/min**, coverage IEX
to all US exchanges and Indicative to OPRA, and the 15-minute historical
limitation lifted on both. Five further things were checked, and four of them
change the design:

1. **Only the *market-data* bucket goes to 10,000/min. The trading bucket
   stays at 200/min.** Alpaca's own support material states the trading
   endpoints are throttled at 200 requests per minute per account, and that
   Algo Trader Plus raises market data to 10k/min *"while the trade endpoints
   are still limited to 200/min"*. `ratelimit.py` already holds one
   `TokenBucket` per host for exactly this shape, so the upgrade raises the
   `data.alpaca.markets` bucket and leaves `paper-api.alpaca.markets`
   untouched. Everything on the trading host keeps its Phase 2 cadence:
   account, positions and activity stay at 15s, and the chain's contract-terms
   join — which is where open interest lives — is still on the tight bucket.
   *"REST goes to 10,000/min"* is true of half the app, and reading it as all
   of it is the upgrade-day error that surfaces as a slowed Activity page
   nothing logs.
2. **The feed is in the websocket URL path** —
   `wss://stream.data.alpaca.markets/{version}/{feed}`. So
   `ALPACA_STOCK_FEED_REALTIME=iex→sip` names a **different endpoint** rather
   than a subscribe parameter: the change takes effect on reconnect, `/v2/iex`
   and `/v2/sip` are separate endpoints for the one-connection-per-endpoint
   rule, and a process cannot upgrade its live socket in place.
3. **The server echoes the full subscription set.** A `subscribe` is answered
   with `{"T":"subscription","trades":[…],"quotes":[…],"bars":[…],…}` listing
   every symbol per channel. That acknowledgement, not any constant in this
   repo, is the authoritative answer to *"was anything truncated"* — see 20.4,
   where it becomes the backbone of the design rather than a nicety.
4. **The option stream carries only `trades` and `quotes`.** No greeks, no
   implied volatility, and `*` is refused for option quotes in the vendor's
   own words — *"there are simply too many of them."* OPRA changes the
   *content* of a quote and adds no analytics channel, which is what 20.5
   turns on.
5. One $99/mo plan appears to cover both halves — the pricing page lists a
   single Algo Trader Plus tier carrying real-time OPRA options *and*
   unlimited-symbol websockets, while the docs present equities and options as
   two tables. Recorded as **verify on the first invoice**, not as a fact.

Two places where the **marketing page contradicts the docs table**, both in
the dangerous direction, both resolved in favour of the docs: `alpaca.markets/data`
says *"Unlimited symbols"* for websockets with no options carve-out, and
*"Unlimited API calls"* where the table says 10,000/min. Whoever reads the
price list on upgrade day rather than the subscription table will wire the
option stream to a sentinel. That is precisely 20.4.

#### 20.1 The equity stream takes the whole Markets universe; the poll becomes a reconciliation sweep at 5s / 30s

The 30-symbol equity budget is what forced decision 18's split between ~22
streamed rows and a 400ms poll for the remainder. With the cap gone the split
goes with it: **every symbol in `MARKET_QUOTES` is subscribed** — 26 stock rows
today, plus the ≤8 position underlyings and the Phase 4 recommendation
underlyings, so roughly forty symbols against a sentinel of 10,000. There is
no longer any such thing as a Markets row that is polled because it did not
fit.

**The poll is retired as the freshness floor and kept as a reconciliation
sweep: 5s foreground, 30s background, still stopped when hidden.** Those are
different jobs, and the second one is worth keeping.

- *Retired as the floor*, because a 400ms poll behind a pushed price cannot
  make anything fresher. Decision 18's rule 2 resolves the two writers on the
  vendor's `at`, so every one of those 150 requests a minute is a write that
  can only lose the comparison — cost with no information, and 150 chances a
  minute to get the merge wrong.
- *Kept as a sweep*, for four things the stream does not do. It is the
  **opening snapshot**, and it has to be: a quote stream delivers on events, so
  an illiquid name prints nothing for minutes and its row would carry no price
  at all until it traded. It carries the fields the stream has no channel for —
  `previousClose` — and it is the request the session-volume and daily-series
  caches sit behind. It is the **degradation path** PRD §7 requires, so a dead
  socket costs freshness rather than the page. And it is the only thing that
  notices a subscription that was acknowledged and then went silent.

**Why 5s and not 1s:** a failed or silent subscription is a condition that
persists until something fixes it, not a transient. Catching it in one second
rather than five changes nothing a human or the watchdog can act on, and the
healthy path is pushed anyway. **Why 5s and not 60s:** the sweep is also the
floor in *degraded* mode, and a minute-old price on a 26-row screener is
visibly wrong. 5s is chosen for the failure case rather than the healthy one,
which is the only case in which it is load-bearing at all. It costs 12/min of
10,000 — 0.12% of budget.

**The three-state hook survives unchanged; two constants change value.**
Decision 18's hidden / foreground / background machinery is exactly right at
either plan: foreground 400ms → **5s**, background 5s → **30s**, hidden
**stopped**, first poll on return still immediate. One diff, no new states.
The coalescing cache stays and its TTL keeps following the foreground interval
automatically, so it becomes a 5s cache — but **its stated reason changes, and
the old one must not be left in the docstring**: at 200/min it existed to stop
two tabs breaching a hard ceiling, and at 10,000/min it exists so that N
clients cost one in-flight vendor request. Same code, different argument; a
cache justified by a limit that no longer binds is a cache someone deletes.

**The sweep is unconditional — never "poll only while the stream is down."** A
conditional sweep has two code paths and its failure mode is that the
condition is wrong: a socket that reports connected while delivering nothing is
exactly the state the sweep exists to survive, and it is the state in which the
condition says *do not poll*. Decision 18's *"the poll is the floor and the
stream is the upgrade"* is unchanged by the upgrade.

**Channels, because unlimited symbols makes the choice free for equities.**
Position underlyings and position contracts subscribe **quotes** — a mark is a
mid, and a trade print on an illiquid contract is not the mark. Markets rows
subscribe **trades**, which is the last price the table actually renders. The
standing question about whether one symbol on two channels spends one slot or
two **dissolves for equities** at unlimited and stays live for options at 1000.

**After a reconnect, re-read a snapshot for every subscribed symbol
immediately.** A gap in pushes is a gap in prices and the sweep's next tick is
up to five seconds away. This is data recovery and it is **not** a resume: rule
9 stands untouched, the watchdog's halt still clears only through
`POST /api/engine/resume`, and nothing in this decision lets a reconnect clear
a halt.

**What still needs polling, and at what rate** — the full post-upgrade table:

| Surface | Mechanism | Cadence | Budget |
|---|---|---|---|
| Position contracts | option WS, `quotes`, OPRA | push | option stream: ≤32 of **1000** |
| Position underlyings | stock WS, `quotes`, SIP | push | equity stream: ≤8, unlimited |
| Every Markets stock row | stock WS, `trades`, SIP | push | equity stream: 26 today, unlimited |
| Visible option chain rows | option WS, `quotes`, OPRA | push | option stream: see 20.2 |
| Markets reconciliation sweep | REST `/v2/stocks/snapshots` | **5s fg / 30s bg / stopped hidden** | data: 12/min of 10,000 |
| Session volume | REST `/v2/stocks/bars` | 60s TTL in session, unchanged | data: ~1/min |
| Average volume, daily series | REST `/v2/stocks/bars` | once per trading date, unchanged | data: ~6/day |
| Intraday chart series | REST `/v2/stocks/bars` | on demand, unchanged | data: 1–5 pages, no longer budgeted |
| Market cap | Finnhub `/stock/profile2` | daily, unchanged | Finnhub's own budget |
| Chain reference data (open interest, terms) | REST `/v2/options/contracts` | on demand, unchanged | **trading: still 200/min** |
| Account, positions, activity | REST | **15s, unchanged** | **trading: still 200/min** |

The last two rows are the ones that will be got wrong: they are on the bucket
that did **not** move.

#### 20.2 The option stream at 1000 quotes: four tiers, and the thing that must never be subscribed

This is the part that needs real design, because 1000 is a ceiling rather than
the absence of one, and the surface that wants option symbols in bulk — the
chain — is the surface Phase 4 is adding.

**`stream.py`'s tiering does not become vestigial at upgrade; it migrates from
the equity stream to the option one.** Stated plainly, because the natural
inference from *"equities go unlimited"* is that the module has nothing left to
do, and the opposite is true: its budget arithmetic stops being exercised on
the equity stream and becomes the only thing standing between a chain view and
a server-enforced limit. Decision 10 already said the module survives as the
graceful-degradation path. It survives for a second reason now — it is the
option stream's rationer, and the option stream is where the scarcity went.

**Decision 18's `MARKETS_VISIBLE` tier still makes sense — for options.** On
the equity stream its job disappears, because there is no longer a subset to
choose: every row is subscribed, so *visible* and *quoted* are the same set.
On the option stream its job is the whole design: the client says which chain
rows are on screen, debounced on a settled viewport, and the server treats it
as input to the **lowest** priority and nothing else. Decision 18's rule holds
verbatim and matters more here than it did there — a client-supplied list can
never outrank a position contract, because a client that could evict a held
contract from the stream could make a position mark stale by scrolling.

**Keep that tier's name page-scoped, not instrument-scoped.**
`MARKETS_VISIBLE` survives this migration and `MARKETS_VISIBLE_STOCK` would
not. `SubscriptionPriority.label` appears in log records and in the API's drop
reports, and renaming a stable label to chase a shade of meaning is churn in
the audit trail. This is a constraint on decision 18's implementation, not a
reopening of it.

**The four tiers and the arithmetic at the new cap:**

| Tier | Option stream, cap **1000 quotes** | Equity stream, cap = sentinel |
|---|---|---|
| 1 `POSITION_CONTRACT` | ≤32 — 8 positions × 4 legs | — |
| 2 `POSITION_UNDERLYING` | — | ≤8 |
| 3 `RECOMMENDED_TRADE` | ≤4 legs × N candidates | N underlyings |
| 4 `MARKETS_VISIBLE` | the chain rows on screen | every Markets row (26) |

`1000 − 32 − (4 × N)` is what tier 4 may spend, and **tier 3 must be bounded
by a served top-N**. At N = 25 that is 100 contracts and leaves **868** slots
for chain rows; at N = 100 it is 400 and leaves 568. Either fits. An
*unbounded* candidate list does not: tier 3 outranks the chain by design, so
an unbounded tier 3 starves it silently rather than visibly. Assume N ≤ 25
until Phase 4 says otherwise, and bound it at the recommendation endpoint
rather than at the stream — the stream should be rationing a set someone
already decided to show.

**Measured chain sizes, so the headroom is not a guess.** From recorded
fixtures: one expiry inside the default ±15% strike band is **26 contracts**
(`tests/fixtures/alpaca/option_chain_nvda_dated.json` — 13 strikes × 2 rights).
The *unbanded* near expiry is **at least 160** across 80 strikes and still
paginating (`option_chain_nvda_page1/2.json`, 100 each with `next_page_token`
set on both). The window the chain route serves by default is
`DEFAULT_MAX_DTE = 60` with `DEFAULT_MONEYNESS_PCT = 15`, which on a
weekly-listed name is roughly eight to ten expiries: **~200–300 contracts,
estimated rather than measured.** A screenful of chain rows is 20–60.

So the default window fits inside the headroom — and the design still
subscribes **only the rows on screen**, for three reasons. The window is a
*server* default the client may widen to `max_dte=365` and `moneyness_pct=100`,
where a liquid name is thousands of contracts. The vendor refuses `*` for
option quotes precisely because chains are enormous. And when the local count
and the server's metering disagree, being wrong by tens is recoverable and
being wrong by thousands is a `405` on every re-plan.

**The chain tier's unit is one contract, never one chain.** A
`SubscriptionUnit` is all-or-nothing over its symbols, which is exactly right
for a spread — three live legs and one stale one produce a net that ticks while
being neither figure — and exactly wrong for a ladder. A chain row's quote is
independently meaningful, so a 230-symbol unit buys nothing and costs the
graceful degradation: it would trip `DropRule.EXCEEDS_CAP` on a widened window
and blank the **entire** chain rather than trimming its tail. One unit per
visible row, in the client's display order, and the strict prefix cut then
drops the far end of what is on screen.

**Do not re-rank the chain tier by moneyness or liquidity.** It is the obvious
improvement — spend the last slots on the contracts that matter — and it breaks
determinism: a ranking keyed on spot re-orders between polls, every re-order is
a resubscribe, and every resubscribe is a gap in the marks the socket was
opened to deliver. The caller's order is preserved exactly within a tier, and
the caller's order is the ladder as displayed.

**Two numbers that are equal and unrelated, and must not be conflated.** The
provider's `_OPTION_CHAIN_PAGE_LIMIT = 1000` is a REST page size; the option
stream's cap is 1000 *quotes*. *"One page fits the stream"* is a sentence
someone will eventually write, and it is a coincidence between two unrelated
constants. Never derive either from the other.

**At the new cap `EXCEEDS_CAP` becomes unreachable** for units built by the
three constructors — the widest is a four-leg spread — and `NO_ROOM` becomes
reachable only through a caller that hands over hundreds of contracts at once.
Worth stating, because it means any `EXCEEDS_CAP` seen after upgrade is a
caller bug rather than a busy book, and the two drop rules stay worth telling
apart.

#### 20.3 150ms is affordable and still not worth doing — recommendation: do not build it

The original ask was ~150ms in the foreground. At 10,000/min that costs
400/min, about **4% of budget**, and decision 18's objection — that it breaches
a hard server-side limit — is gone. It should still not be built, and this is a
recommendation rather than an open question.

A 150ms poll behind a streamed universe cannot deliver a price sooner than the
push that already delivered it. It can only race the push and lose decision
18's `at` comparison, 400 times a minute, across every symbol at once. What it
*would* buy is a second writer into `underlyings` running an order of
magnitude hotter than the thing it cannot beat, a full-universe snapshot
payload 400 times a minute of bandwidth and parsing for no new information, and
a much larger surface on which to get the field-level merge wrong. The
freshness question on Algo Trader Plus is not *"how often do we ask"*; it is
*"is the socket alive, and is our subscription set the one the server
acknowledged"* — which is 20.4 and the watchdog.

**Say the substitution out loud, because it reads as a regression against what
was asked for.** The answer to *"plan for faster updates on the upgraded
plan"* is that the surfaces that were the point of the request get **push**,
and the poll behind them gets **twelve times slower** than the number decision
18 landed on. Faster where it is read, slower where it is redundant. Decision
18 refused 150ms because it was impossible; decision 20 refuses it because it
is pointless, and those are different refusals worth recording separately.

#### 20.4 `UNLIMITED_STREAM_SYMBOL_CAP` is equities-only, and structurally so

`runtime.UNLIMITED_STREAM_SYMBOL_CAP = 10_000` is a sound sentinel for a limit
Alpaca documents as absent, and applying it to the option stream would
subscribe past a limit the server enforces — the silent truncation `stream.py`
exists to prevent, arriving from the other direction. The marketing page's flat
*"Unlimited symbols"* is what makes this the single most likely thing to be
wired wrong on upgrade day.

**Where it may be used:** as the `cap` argument for a `plan_subscriptions` call
planning the **equity** stream on the paid plan, and nowhere else. It is
already only reachable through `runtime.py`, which owns the plan lookup and the
environment.

**Where it may not be used:** anywhere on the option path — not as a cap, not
as a default, not inside a `max()`, and not in a test that then asserts a plan
of several hundred contracts was admitted. The option cap on the paid plan is a
literal **1000**, named for what it is.

**Discipline is not the mechanism. Three structural things are.**

1. **No singular cap name survives, in `runtime.py` either.** Decision 17
   already retires `STREAM_SYMBOL_CAP` for two stream-named constants on the
   grounds that *"the single unqualified name is the bug's habitat."* The same
   argument convicts `EngineRuntime.stream_symbol_cap` and
   `stream_symbol_cap_for_plan`, both of which return one `int` for a
   two-budget world. They become a single lookup returning a **pair** — a
   frozen `StreamBudget(equity=…, option=…)` constructed only by
   `stream_budget_for_plan(plan)` — so no call site ever picks a number and the
   option field cannot be reached by a caller who was thinking about equities.
2. **A test that the sentinel never reaches the option field**, over every plan
   value the app accepts: `stream_budget_for_plan(p).option !=
   UNLIMITED_STREAM_SYMBOL_CAP` for all `p`, and `== 1000` on the paid plan.
   Cheap, and it fails on exactly the mistake being guarded against.
3. **The server's acknowledgement reconciles the plan, and outranks every
   constant in this repo.** After each subscribe, compare
   `SubscriptionPlan.subscribed` against the `subscription` message's
   per-channel lists. A symbol in the plan and absent from the acknowledgement
   is a drop: it counts into *"N symbols not streamed"* and is logged with its
   rule and inputs, exactly like a locally-refused unit. A `405 symbol limit
   exceeded` is treated as an authoritative cap **correction** — lower the
   effective option cap for the session, re-plan, log it — and **never** as
   permission to raise one. This is what makes 1000 safe even if the metering
   unit differs from our count, which is a standing *Not verified* item that
   research cannot close and this can.

Item 3 is cheap enough to build at Phase 2 step 8 alongside the WS fan-out, and
decision 20 does not require it earlier: at Basic it is a latent safety net, and
at 1000 option quotes it is the design.

#### 20.5 The embargo lifts: a real-time mark is worth pushing in a way a 15-minute-old one is not

Under Basic the option stream's content is the `indicative` feed — the vendor's
own description is a free derivative feed with 15-minute delayed trades. So the
entire push path for option marks, which has no cadence to tune and therefore
looks maximally fast, has been delivering a price whose *content* is a quarter
of an hour behind. Three consequences at upgrade:

- **The mark becomes a measurement.** An OPRA NBBO mid is the price the
  contract is actually quoted at, so the position rows, the P&L total, the
  payoff curve's y-intercept and every risk figure derived from a mark stop
  carrying an undisclosed 15-minute lag. This is decision 19's argument
  restated at the level of one number: pushing a stale price quickly is not
  freshness.
- **The stale pill starts meaning what it says.** `LiveStatus`'s ~15s
  threshold on an option mark is, on Basic, a statement about the *socket*
  rather than about the *price* — the socket can be perfectly healthy while
  every number it carries is fifteen minutes old. At OPRA the two readings
  converge and the pill is honest without a footnote.
- **Session volume can come from a live bar**, and the intraday series may run
  to the current minute rather than stopping fifteen minutes short and
  appending a live point. `UnderlyingChart.tsx` renders that appended point
  today with a comment naming the embargo; both the comment and the append are
  revisited at upgrade, and the append is deleted only if the series really
  does reach the current minute. Measure before deleting.

**The one thing that does not change is the one people will assume does:
greeks and implied volatility are not on the wire.** The option stream carries
`trades` and `quotes` and nothing else, verified above. Decision 10's
derivation path is therefore not made redundant by OPRA; it changes role.
Vendor analytics arrive only on a REST snapshot, so a streamed contract's
vendor IV is as old as the last chain read while its *mark* is current, and a
locally-derived IV re-solved on each push is the only figure that keeps step
with the price beside it. Refine decision 19's *"`impliedVolatility`, `greeks`
and `open_interest` arrive from OPRA rather than being partly derived"*: the
vendor's fields are themselves a Black-Scholes solve — decision 10 quotes
Alpaca's own OpenAPI document saying so — so the upgrade buys **fresher inputs
and probably wider coverage, never a measurement**. `iv_source` stays, the
`derived` branch stays, and whether OPRA widens the vendor's solve coverage
beyond the 19-of-100 measured on `indicative` is unverified and cheap to
measure on day one.

#### 20.6 The vestigial list: delete, rewrite, or keep — and which is which

Decision 19 flagged the constraints that become misleading. This is that list
at file level, split by what must actually happen to each, because *"flagged"*
is not specific enough to act on and the wrong action here is either a sentence
that lies to the reader or a deleted degradation path.

**Delete.** Each of these is a claim about a limit that no longer exists, and
leaving it in place makes a lifted cap look permanent.

- The **equity** half of every *"30 symbols"* sentence, after decision 17 has
  already narrowed each from *"the websocket"* to *"the equity stream"*:
  `corollary/engine/stream.py`'s module title and opening paragraph,
  `corollary/engine/runtime.py`'s `stream_symbol_cap_for_plan` docstring,
  `corollary/data/providers/interface.py:418`,
  `corollary/engine/execution/interface.py:678`,
  `web/src/hooks/useMarketPoll.ts:8`, `web/src/hooks/useLiveTick.ts:13`,
  `web/src/hooks/useNewsPoll.ts:9`, four comments in `web/src/lib/store.ts`,
  and `web/src/lib/mockData.ts:1413`. The sweep decision 17 runs in Phase 2
  runs again here, against a different word.
- The **Markets viewport hint's equity binding** — the client message, its
  debounce, and the server's equity producer for that tier. The *mechanism* is
  not deleted: 20.2 re-points it at chain rows. What is deleted is the reason
  it ever applied to stocks.
- `MAX_RESPONSE_POINTS`'s round-trip paragraph in
  `corollary/api/routes/markets.py` — specifically the sentence costing five
  pages against *"the shared 200/min `data.` budget, of which decision 18 …
  already spends 150/min"*. At 10,000/min with the sweep at 5s, that whole
  paragraph is arithmetic about a constraint that no longer binds. The
  **constant stays**: it bounds bytes on the wire to the client, which no plan
  changes.
- Decision 18's *"300ms would be 200/min exactly"* reasoning wherever it has
  been copied into a code comment. The floor is no longer a floor.
- `corollary/ratelimit.py`'s `DEFAULT_REQUESTS_PER_MINUTE = 200` as a
  *default*: the data host's rate becomes plan-derived and the trading host's
  does not, so one module-level default covering both is the thing that makes
  the asymmetry invisible. The per-host override the module already supports is
  the mechanism; what goes is the assumption that 200 is the right starting
  number for every host.

**Rewrite, do not delete** — these are on-screen sentences, and the surface
still needs a sentence:

- `web/src/components/LiveStatus.tsx:81`, the poll tooltip: *"Polled snapshots
  across every quoted symbol. Chains are not streamed — the websocket is capped
  at 30 symbols and those are spent on open positions."* Every clause is false
  at upgrade. It becomes a sentence about what is streamed and what the sweep
  is for.
- `web/src/components/LiveStatus.tsx:42`, the module comment's *"the stream is
  a 30-symbol websocket scoped to open positions."*
- `web/src/pages/Markets.tsx:869`: *"These are snapshot reads, taken when you
  open the page and when you refresh — every option contract is its own symbol
  and the socket's 30-symbol budget belongs to open positions, so nothing here
  streams."* At upgrade the stock table streams in full and the visible chain
  rows stream, so this paragraph inverts. It should keep saying which rows are
  pushed and which are swept: a page that explains its own freshness is the
  reason the stale pill is legible.
- `web/src/lib/settings.ts`'s `feedWarning`, which today reasons that
  *"real-time IEX gets no warning: it is the only thing the free plan serves
  live."* On the paid plan, real-time IEX becomes a choice to read 2.5% of
  volume while paying for 100% — which is exactly when it deserves a warning.
  `feedWarning` takes the plan and gains that branch; `feedOptionsFor` already
  keys off the plan and needs nothing.

**Keep, explicitly, against the instinct to tidy up:**

- `STREAM_SYMBOL_CAP`'s equity value of 30 and the whole Basic branch. A
  subscription can lapse, `ALPACA_DATA_PLAN` can read `basic` again tomorrow,
  and `data_plan`'s unknown-value fallback is deliberately the restrictive
  direction. Deleting the Basic branch turns a lapsed card into a silent
  server-side truncation.
- The reconciliation sweep, the coalescing cache, and decision 18's
  `at`/`source` provenance with its field-level merge. All of them exist
  because two writers share one quote map, and the upgrade adds writers rather
  than removing them. **Decision 19 listed the coalescing cache among the
  vestigial items; that is right about its rationale and wrong about the
  mechanism.** The reason it was built — keeping a 200/min ceiling out of reach
  — does become vestigial, and 20.1 states the replacement reason, which is
  that N clients should cost one in-flight vendor request whatever the
  ceiling.
- The IV/greeks derivation path and `iv_source: 'derived'` — 20.5.
- `engine/stream.py` in its entirety — 20.2.

#### 20.7 The upgrade is a restart, and a restart comes up halted

Sequencing, because this is the one item with a rule attached. The realtime
feed lives in the socket URL, so new feeds take effect on reconnect, and the
clean way to reconnect three sockets and two caps at once is to restart the
engine. Cold start comes up **halted until the opening snapshot succeeds** and
always in **Paper** (rule 5), and the halt clears only through
`POST /api/engine/resume` (rule 9). So upgrade day is: set the four environment
variables, restart, watch the opening snapshot, confirm the `subscription`
acknowledgement matches the plan on all three sockets with no `405`, then
resume **by hand**. Nothing here may auto-resume anything, and the temptation
will be unusually strong because the restart is planned rather than a fault.

Measure two things during that first session, because both are cheap then and
archaeology later: the **inbound message rate** at the full subscription set,
and the vendor's IV/greeks coverage on OPRA. The first is the limit most likely
to bind next — symbol count stops being scarce for equities while message
volume does not, and 26 SIP names plus 32 OPRA contracts plus a chain view is a
materially different inbound rate from eight IEX symbols on one asyncio loop.

#### Not verified (decision 20)

- **Whether one $99/mo charge covers both the equities and the options
  halves.** The pricing page reads as one plan; the docs present two tables.
  Check the first invoice, not the marketing page.
- **Whether OPRA widens Alpaca's own IV/greeks coverage** beyond the 19-of-100
  and 12-of-100 measured on `indicative` in decision 10.
- **The option stream's metering unit at 1000.** The table still says *"1000
  quotes"* where equities say *"symbols"*. 20.4's acknowledgement
  reconciliation is the answer that does not depend on knowing.
- **The inbound message rate at the full subscription set**, and therefore
  whether the coarser stock channels (`bars`, `dailyBars`) should carry the
  Markets table instead of `trades`. Deferred deliberately — a third writer
  into `underlyings` for a slow-moving column is a bad trade until the message
  rate says otherwise.
- **Whether `/v2/stocks/snapshots` caps its `symbols` list.** Carried forward
  from Phase 2 unchanged, and no longer load-bearing: at 5s, a chunked request
  costs nothing.
- **Whether the equity stream's "Unlimited" holds at hundreds of symbols.**
  Only the `405` is authoritative, and nothing in this design goes past ~40.

#### Rejected

Rejected (20.1): **retiring the poll entirely.** It is the opening snapshot for
a symbol that has not traded, the only source of `previousClose`, and PRD §7's
graceful-degradation path. A push-only design renders a screener with blank
cells until each row happens to print.

Rejected (20.1): **sweeping only the symbols the stream has gone quiet on.**
The endpoint takes a list and costs one request either way, so it saves nothing
and opens a hole the moment the quiet-detection is wrong. Decision 18's
rejection, for decision 18's reason.

Rejected (20.1): **making the sweep conditional on stream health.** The state
it exists to survive is a socket claiming health while delivering nothing —
which is the state in which the condition disables the sweep.

Rejected (20.1): **taking session volume from the `dailyBars` channel.** It
would work — cumulative daily aggregates arrive each minute mark — and it adds a
third writer into the one quote map for a column that moves slowly, against a
60s REST refresh costing ~1/min of 10,000. Revisit only if the message-rate
measurement makes REST look expensive, which is the opposite of what is
expected.

Rejected (20.2): **subscribing the whole chain window the API returns.**
~200–300 contracts on a default window, thousands on a widened one, and the
vendor refuses `*` for option quotes because chains are enormous. The client's
viewport is the only bounded set on that page.

Rejected (20.2): **one `SubscriptionUnit` per chain.** All-or-nothing is right
for a spread and wrong for a ladder: it blanks an entire chain on
`EXCEEDS_CAP` where trimming the tail degrades correctly.

Rejected (20.2): **ranking the chain tier by moneyness or liquidity.** It
spends the last slots better and re-orders on every spot move; every re-order
is a resubscribe, and every resubscribe is a gap in the marks.

Rejected (20.2): **renaming `MARKETS_VISIBLE` when its producer changes
instrument.** The label is in log records and drop reports, and the name
already says which page rather than which asset class.

Rejected (20.2): **giving the Phase 4 scanner universe a stream** now that
equity symbols are unlimited. The scanner is deterministic by requirement —
same inputs, identical output — and inputs that arrive by push are inputs whose
value depends on arrival timing. PRD §7 keeps the scanner on polled snapshots,
and that is a correctness argument rather than a budget one.

Rejected (20.3): **150ms, or any sub-second poll, on the upgraded plan.** 4% of
budget, zero information, and a second writer racing a push it cannot beat.
Recommended against rather than left open.

Rejected (20.4): **reading the option cap from the environment**, next to the
feed names. Decision 17's argument unchanged: a cap in the environment is a cap
someone can raise without paying, and the failure is silent truncation.

Rejected (20.4): **one `int` cap with an asset-class argument**, e.g.
`cap_for(plan, "option")`. It keeps the singular name that was the bug's
habitat and moves the mistake from picking a constant to passing a string.

Rejected (20.6): **deleting the Basic branch of the stream budget.** A lapsed
subscription would become a silent truncation, and `data_plan` already reads
unknown values as Basic on purpose.

Rejected (20.6): **scaling `MAX_RESPONSE_POINTS` by the 2.46× fetch ratio.**
Already rejected where the constant lives, for a reason the upgrade does not
touch: the ratio is a property of the intraday path, and scaling by it
penalises the daily path, which is the default and the common one.

---

## Three rule reinterpretations, approved

### The vendor surface gains a read half

CLAUDE.md names two files as the entire Alpaca surface. Reading positions, orders, activities and balances is neither market data nor order placement, so the rule does not say who owns it. The trading API *is* the broker, so `BrokerInterface` splits:

- **`BrokerAccount`** — account, positions, orders, activities. Implemented in Phase 2 by `engine/execution/alpaca.py`.
- **`BrokerExecution`** — submit, cancel, replace. **Does not exist until Phase 6.**

API routes depend on `BrokerAccount` only, so `submit_order` is not in a type they can reach. That keeps rule 1 structural rather than disciplinary. Rule 2 is untouched: the backtest worker still gets a scrubbed environment and `SimBroker`.

### `Decimal` serializes as a JSON number

Money is `Decimal` everywhere it is *computed* — Alpaca's strings parse straight to `Decimal` on ingest, money DB columns are `Money` (see Database below — on SQLite that is TEXT, not `Numeric`, and it is stricter rather than looser), the matcher and engine are `Decimal` throughout. The API boundary is a display boundary and serializes to a JSON number.

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

**Rewritten 2026-09-14** by decisions 17 and 18. The previous table rationed
one 30-symbol pool across both streams and polled Markets at a flat 2s; both
are superseded.

| Feed | Mechanism | Cadence | Scope | Budget spent |
|---|---|---|---|---|
| Position contracts | option WS | push | every leg of every open position | option stream: ≤32 of **200 quotes** |
| Position underlyings | stock WS | push | the underlyings behind them | equity stream: ≤8 of **30 symbols** |
| Order and fill events | `trade_updates` WS | push | active account | its own endpoint, no symbol budget |
| Markets, visible rows | stock WS | push | whatever rows are on screen | equity stream: the **~22** slots left over |
| Markets, every row | REST `/v2/stocks/snapshots` | **400ms foreground / 5s background / stopped when hidden** | every quoted symbol, one request | data bucket: **150/min** at 400ms |
| Session volume | REST `/v2/stocks/bars` | 60s TTL during a session, trading-date key when closed | every quoted symbol | data bucket: ~1/min |
| Daily volume series, chart series | REST `/v2/stocks/bars` | once per trading date | every quoted symbol | data bucket: ~6/day |
| Intraday chart series | REST `/v2/stocks/bars` | on demand, **no cache** | one symbol, or the table's set | data bucket: 1–5 pages per request |
| Option chain | REST snapshots + contracts | on demand | one underlying | data bucket: 1 spot + ≥1 page; trading bucket: ≥1 |
| Account / positions / activity | REST | 15s, and on demand | active account | trading bucket: ~12/min |

**Two token buckets, not one.** `data.alpaca.markets` and
`paper-api.alpaca.markets` each carry their own 200/min, and
`corollary/ratelimit.py` holds one `TokenBucket` per host for exactly that
reason. The account trio and `/v2/options/contracts` are on the **trading**
host; every snapshot and bar is on the **data** host. The two never compete.

**The data bucket, counted rather than estimated.** `GET /api/markets/stocks`
issues **one** Alpaca request per cycle — `stock_snapshots` puts every symbol
in a single `symbols=` list and the endpoint is not paginated. So 400ms is
150/min, the session-volume refresh adds ~1/min, and the daily series is a
once-a-day cost already paid. That leaves **~49/min** for on-demand chains at
two data-bucket requests each: roughly twenty-four chain loads a minute, which
no human reaches. 300ms would be 200/min exactly — at the ceiling, with
nothing left for a chain — which is why the floor is 400 and not 300.

**The one line in that table bounded by a constant rather than by a cadence is
the intraday series**, and it is bounded loosely: the regular-hours filter runs
after the fetch, so a permitted request costs up to about five vendor pages
rather than the two `MAX_RESPONSE_POINTS` used to claim. See *The series
ceiling counts served points, not fetched bars* under Constraints. It is
on-demand and cacheless, so the cost is per chart expansion; at human click
rates it fits the ~49/min left over, and it is the first thing to reprice if
that headroom is ever spent on something else.

**Two symbol budgets, enforced separately.** `engine/stream.py` plans **one
stream at a time**: option units against 200, equity units against 30. A unit
never spans the two, and `plan_subscriptions` is called twice rather than
taught to hold two budgets — see decision 17. Position contracts come first,
then position underlyings, then Phase 4 recommendations, then Markets'
visible rows; the tail is dropped, logged with its rule and inputs, and
surfaced as *"N symbols not streamed"*. Never silently.

**The poll is the floor and the stream is the upgrade.** Every Markets symbol
is polled whether or not it is also streamed, so a row falling off the stream
degrades from push to 400ms rather than to nothing, and a churning viewport
costs freshness rather than correctness. Requesting only the un-streamed rows
would save **zero** requests — it is one request either way — and would open a
hole on every dropped subscription.

### Database

SQLite, WAL, one writer. Ten tables.

**Money columns are `Money`, not `Numeric` — a deliberate deviation from CLAUDE.md's letter, made to keep its intent.** SQLite has no exact-decimal storage class and applies NUMERIC *affinity* to the declared type, so a `Numeric` column converts `'7.5'` to an IEEE double on the way in; SQLAlchemy warns as much. `corollary.db.types.Money` stores TEXT and converts back to `Decimal` on read, so no float touches the path.

The cost is that SQL compares that text **lexicographically**. Against the five seeded ceilings (7, 20, 8, 25, 40), `MAX(value)` is `8`, `MIN(value)` is `20`, `WHERE value > 10` matches all five, and `ORDER BY value` gives 20, 25, 40, 7, 8 — every one a plausible number, none an error. `select(RiskLimit).where(RiskLimit.value < computed_risk)` therefore approves a 35%-of-account trade against the 40% ceiling and calls the 7% per-trade limit unbreached. So `Money` **raises `MoneyComparisonError`** on every ordering, equality, aggregate and arithmetic operation instead of answering; comparison happens in Python on `Decimal`, via `db.seed.risk_limits`.

- `risk_limit(key, value Money)` · `data_feed(key, value)` · `notification_route(event, channel, enabled)` — typed separately rather than one key/value table, because the limits need an exact decimal and the other two do not. `value` also carries `ck_risk_limit_value`, a text-shape CHECK that rejects `Infinity`, `NaN`, negatives, zero and absurd magnitudes: rule 4 puts enforcement on the server, and `validateRiskLimit` in `settings.ts` is the client. Python validation (`models.validate_risk_limit`) mirrors the per-limit ranges the Settings page shows.
- `audit_log(id, at, category, field, previous_value, new_value)` — spans all three, per §8.7's *"one log rather than three"*.
- `engine_state(id=1, halted, halted_reason, halted_at, t0)` — singleton.
- `fill(id, account, activity_id UNIQUE, order_id, symbol, side, position_intent, qty, price, at)` — raw activities. Needed because `page_size` maxes at 100 and re-fetching all history per request is untenable.
- `realized_trade(id, account, symbol, opened_at, closed_at, qty, open_price, close_price, pnl Money, pnl_pct Money, close_kind)` — matcher output. `close_kind` ∈ `fill` | `expiry` | `exercise` | `assignment`. `pnl` is signed, so "biggest loser" is a Python sort over the loaded rows, not `ORDER BY pnl`.
- `mleg_group(id, account, order_id, opened_at, net_price)` + `mleg_leg(group_id, symbol, ratio, side, position_intent)` — the grouping evidence.
- `notification(id, at, event, severity, account, title, body, read_at, dismissed_at)` — engine events only this phase.

No `equity_snapshot`: the curve comes from Alpaca and the marker from `engine_state.t0`.

Ingestion runs on startup and on an interval — pull activities newer than the last `transaction_time`, upsert, join to orders for `position_intent`, re-run the matcher incrementally. Idempotent on `activity_id`.

### The FIFO matcher

Pure function, fill sequence → realized trades. No I/O, `Decimal` throughout.

An open-lot queue per contract symbol. `*_to_open` pushes a lot; `*_to_close` pops FIFO, emitting a trade per matched slice. Intent comes from the order, never from `side`: the probe found `side` takes **three** values — `sell_short` opens a short, `sell` closes a long, and `buy` is *both* BTO and BTC. Two of the four actions are indistinguishable without the join. Long P&L is `(close − open) × qty × multiplier`; short inverts. `multiplier` is per contract from the contracts endpoint, cached.

`OPEXP` closes remaining lots at zero — a full loss on a long, the full credit kept on a short. Per the option-event finding above, `OPEXP` is the **OTM case only**: Alpaca auto-exercises ITM contracts absent a DNE instruction, so an ITM expiry arrives as an `OPEXC` pair and never reaches this branch.

`OPASN` and `OPEXC` close at the strike, **read from the paired `OPTRD` row's `price` or parsed from the OCC symbol — never from the event row's `net_amount`, which is zero.** A matcher that trusts `net_amount` here books every exercise as a total loss. They emit a flag; Corollary is not a stock app, so the resulting shares are named, not tracked.

The matcher also normalises two conventions into one: `FILL` rows carry an unsigned `qty` with a separate `side`, while non-trade rows carry a **signed** `qty` and no `side` at all. Normalise on ingest, so the matcher itself sees one convention.

Fees attribute by `order_id` where one is present. **Non-trade activities have no `order_id`** — `group_id` is the only linkage they get — so fee attribution for expiry, assignment and exercise runs through `group_id`, and anything still unattributed is reported separately rather than dropped.

`pnl_pct` denominates on cost basis — `open_price × qty × multiplier` — so a short's basis is the credit received, matching `orders.ts`'s negative `openUnitValue`.

### Multi-leg grouping

Pure. Broker positions plus mleg history → logical positions.

The join is **two-hop**, per the probe: a fill carries its *leg's* order id, so
reaching the parent `mleg` order means `GET /v2/orders?nested=true` and a
leg-id → parent-id map built from `legs[]`. A one-hop join groups nothing at
all, and does so silently.

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

**PRD: the five listed amendments are applied as of 2026-09-11.** Approved and
written that evening rather than held for step 10, because the evidence behind
four of them was in hand and reconstructing it later is how a doc pass turns
into archaeology.

- ~~**PRD §8.1** — equity curve is Alpaca's, with a t₀ marker.~~ **Done 2026-09-11.**
- **PRD §12** — one process, resolved.
- ~~**PRD §8.2** — the realized-P&L ledger exists; expiry and assignment are ledger states.~~ **Done 2026-09-11**, as three sub-bullets under the header stats.
- ~~**PRD §8.6** — *"keeps no ledger"* scoped to cash transfers, and *"Paper is a margin account at 2× cash"* dropped.~~ **Done 2026-09-11.** The scoping sentence now states the boundary in both directions: money moved is the broker's record, money made is Corollary's arithmetic. The multiplier is read from the account object; this one reports `4`.
- ~~**PRD §8.4** — the "highest IV" and "highest open interest" screens.~~ **Done 2026-09-11**, per decision 10's amended finding: both screens survive, vendor analytics pass through where they exist, derived values are labelled as derived, and a null open interest stays null and never ranks.
- ~~**PRD §9** — the provider-supplied sentiment tier.~~ **Removed 2026-09-11, on evidence**, and the section is now two tiers. Not on the original list, because the original list assumed the tier worked. Probed on this project's keys: Alpaca `/v1beta1/news` returns `source: benzinga` with **no sentiment field**; Finnhub `/company-news` returns 245 NVDA articles with **no sentiment field**; Finnhub `/news-sentiment` answers **502 (HTML)** on three attempts across two symbols while `/quote` returns 200 on the same key. The control established the key was valid and the vendor up; **the account holder then confirmed the endpoint is paywalled**, which is what the 502 was — an unentitled request answered with a gateway error instead of a JSON 403. §7's data-source table was updated in the same pass.

  This is the **second** missing field in this project to turn out priced rather than absent, after OPRA's IV and greeks in decision 10. The habit that follows: when a documented field does not arrive, check the bill before concluding the data does not exist. The tier is removed as *unbought* — restoring it is a $11.99–99.99/mo Finnhub Premium decision, not a vendor hunt.

**Follow-up this opens, deliberately not done in Phase 2:** `web/src/lib/types.ts`
still declares `SentimentTier = 'provider' | 'rules' | 'llm'` with a
`SENTIMENT_TIER_DETAIL` entry reading *"Tier 1 — a score shipped by Finnhub or
Alpaca, published as-is"*, which the PRD now denies. Eight fixtures in
`mockData.ts` carry `tier: 'provider'` and `news.test.ts:212` asserts all three
tiers appear. That is Phase 3's News surface and out of scope here by this
spec's own exclusion list, so it is recorded rather than fixed — but it is a
live contradiction between the PRD and shipped types, and whoever builds the
News page owns it.
- **CLAUDE.md** — the vendor surface gains `BrokerAccount`; layout gains `engine/{ledger,grouping,runtime,stream}.py`, `api/routes/`, `api/schemas.py`; `CONTRACT_MULTIPLIER` is a fixture default and real multipliers are per contract; options level is read from the account, not hardcoded to 3.
- **CLAUDE.md, layout — `corollary/wire.py` is not in the module layout above and
  needs naming.** Added during step 4 because *both* vendor HTTP files have to turn
  prices into exact `Decimal`, nanosecond RFC-3339 stamps into aware UTC datetimes,
  and counts into `int | None`, and the alternative was the broker importing the
  market-data provider's privates. It is vendor-**neutral** by design — no `alpaca`
  import, no `.json()`, and `as_decimal` *raises* on a `float` rather than converting
  one — which is the test `AlpacaCredentials` deliberately fails, so credentials stay
  in `data/providers/alpaca.py` and the broker imports them.
- **This spec, "An option expiring is not a fill" — fee attribution must not run
  through `group_id` alone.** Probed 2026-09-11 while recording step 4's fixtures:
  `group_id` is **null on every non-trade row** on this account. What is present is
  **`execution_id`, on 15 of 19 `FEE` rows** — a *third* field absent from Alpaca's
  published `NonTradeActivities` schema, after `description` and `price`. The
  attribution chain is `order_id`, then `execution_id`, then `group_id`, and anything
  still unattributed is reported separately rather than dropped. `group_id` stays in
  the chain: it is the documented linkage for an option-event pair, which this account
  has never produced.
- **This spec, "There is no realized P&L anywhere" — the composite activity `id` does
  *not* sort chronologically, and neither does `transaction_time`.** The claim that it
  *"sorts chronologically as a string"* holds only for the 17-digit **stamp**. The whole
  id does not: stamps repeat and the UUID half then breaks ties arbitrarily. And
  `transaction_time` does not either at microsecond resolution even with
  `direction=asc` — `…438268` was returned before `…438263`. Alpaca orders by the
  millisecond stamp and the sub-millisecond tail is not a tiebreak. **A FIFO matcher
  must not depend on a global sort.** The invariant that does hold, measured on real
  data: per contract symbol the fills are ordered, and no symbol has two fills inside
  a second.
- **This spec, "The account object has no settlement breakdown" — `crypto_tier` is a
  third integer field**, alongside `options_approved_level` and `options_trading_level`.
  *"Every other numeric field on the account object is a string"* is true of every
  **money** field, not of every numeric one.
- **This spec, "Positions are per contract" — the account has grown since the probe.**
  **13 position rows across 9 logical positions** (4 verticals + 5 singles), not 11/7;
  15 fills (`buy`×9, `sell_short`×5, `sell`×1 — still three values), 19 `FEE` rows and
  1 `JNLC`. `multiplier: '4'`, both options levels integer `3`, and
  `pending_transfer_in`/`out` absent are all re-confirmed.
- **The fixture recorder needs a redaction note: Alpaca embeds the account number in
  free text.** A `FEE` row's `description` reads *"CAT fee for proceed of 15 trades on
  2026-09-10 by PA0EXAMPLE00"* — the account number there is a placeholder, because
  rule 6 covers this document too and the shape is the whole of the point. Field-name
  redaction cannot see inside prose, and the first recording wrote the real one into
  eight fixture rows in plain text before this was caught. Redaction is now a substring
  pass as well as a field pass, the account identifiers are in the scanned-secrets set
  so a miss aborts the run, and a shape-based sweep over every `.py`, `.md` and
  recorded fixture in the tree guards it — widened from the fixture directory alone
  after this very note, and two like it, were found carrying the value they describe.
  Rule 6 is the reason this is recorded rather than just fixed.
- **This spec, "Order of work" step 2 — *"Settings goes server-backed"* is wrong and
  belongs to step 7.** Confirmed 2026-09-11: the three config tables exist and are
  seeded, but `api/__init__.py` is still the Phase 1 health-check app, there is no
  `api/routes/` or `api/schemas.py`, `web/src/lib/api.ts` does not exist, and
  `Settings.tsx` still imports from `mockData`. Nothing serves the tables. Step 2 is
  complete as scoped; the sentence over-claimed.

- **This spec, Constraints — the series ceiling has a fetch/serve asymmetry and
  `1H` cannot show the open.** Added 2026-09-14 out of the regular-trading-hours
  work: `MAX_RESPONSE_POINTS` no longer means *"two vendor pages"*, a permitted
  intraday request can cost ~5, and an hourly session serves six points from
  10:00 ET rather than seven. The constant was deliberately left at 20,000 and
  `1H` deliberately left askable-but-unsuggested; both reasons are in *The
  series ceiling counts served points, not fetched bars*, and the cadence
  consequence is in *Feeds and budgets*. Decision 20 is where the round-trip
  cost stops mattering.
- **PRD §7, to be amended at Phase 4 and not before — the stream-budget
  paragraph's *"Markets-page chains and scanner universe scans run on polled
  snapshots"* needs one clause.** At 1000 option quotes the chain's **visible
  rows** stream at the lowest priority (decision 20.2) while the scanner
  universe stays polled *for a different reason than the budget* — determinism.
  The paragraph's closing sentence, *"the polled path should remain the default
  regardless, so the app degrades gracefully"*, needs no change: decision 20.1
  keeps the poll as an unconditional reconciliation sweep precisely to honour
  it. Not edited now, because PRD §7 currently describes the plan the account
  is actually on.
- **CLAUDE.md, to be amended at Phase 4 — *"Upgrading to Algo Trader Plus …
  sets all three to `opra`/`sip`/`sip` and changes nothing else"* stops being
  true.** It also sets `ALPACA_DATA_PLAN`, moves two stream caps, moves the
  poll constants, raises the `data.` rate-limit bucket and **not** the trading
  one, and deletes or rewrites the on-screen sentences listed in decision 20.6.
  The sentence was accurate when the upgrade was a Phase 6 abstraction; decision
  19 made it a Phase 4 checklist, and decision 20 is that checklist.

---

## Order of work

**Rewritten 2026-09-14 so that this list is the status of record.** The
previous version hid step 8's six sub-steps behind one line and hid step 11
entirely; their real state lived in `.claude/scratch/step8-10-orchestration.md`.
The cost was not hypothetical — a dispatch arrived believing steps 0 through 8
had landed when `corollary/api/routes/ws.py` had never been written, which is
rule 9's dead-man's switch with a proven mechanism and no wire attached.

**`.claude/scratch/` is not tracked, and must not be where status lives.** The
directory is the last entry in `.gitignore`, described there as *"working
memory, not project record"*, and that is the right scope for it. A step's
state is project record: it has to survive a session ending, be readable by
someone who never saw that session, and be checkable against the tree. Working
notes, audit transcripts and hand-off details belong in scratch and should stay
there. **What is done, what is not, and what blocks what belongs here**, changed
in the same commit that changes the thing it describes. A status claim that
exists only in an ignored file is a status claim nobody can review and `git log`
cannot contradict.

Each entry gives the work in one line, then **Status**, **Depends on** and
**Files**. A landed step names the commit, so the list reads as status rather
than as intent. Every entry is marked **Phase 2** or **Phase 4**. Numbers 0–10
are the original sequence and keep their numbers so that older references still
resolve; **11–15 are the work decisions 17 and 18 created**, which until now
existed only inside those decisions and appeared in no list; U1–U10 are Phase 4
and are unchanged.

### Phase 2 — landed

**0. Check whether signing the OPRA agreement is free.** *Done — it is
paywalled*, and resolved as decision 10: derive IV and greeks, report open
interest absent, Markets work unblocked. **The rest of that sentence is
superseded.** This entry read *"buy the plan at Phase 6"* until 2026-09-14;
**decision 19 moved the purchase to Phase 4**, because a scanner that ranks and
sizes candidates crosses decision 10's own *"fine for a column and not fine for
sizing"* boundary before any order does. The Phase 4 checklist is U1–U10 below.

**1. Type extraction and vite proxy** — pure refactors.
- *Status:* landed `2b36bf4`.
- *Files:* `web/src/lib/types.ts`, `web/vite.config.ts`.

**2. DB and Alembic wired; three config tables, audit log, engine state.**
- *Status:* landed `f4786c9`; ledger schema `141eaec`, the `activity_id` SQL
  guard `39aecea`, an explicit 5000ms `busy_timeout` `d3a830d`, migration 0004
  `1824191`. Head is **0004**.
- *Correction, kept so it is not re-derived:* this entry used to end *"Settings
  goes server-backed"*, which over-claimed — the tables existed and nothing
  served them. That work is step 7's. Recorded under *Doc amendments*.
- *Files:* `corollary/db/`, `corollary/db/migrations/versions/`.

**3. `MarketDataProvider` + `AlpacaProvider`** — quotes, snapshots, bars,
chain, contracts; rate limiter, feed config.
- *Status:* landed `508cb5a`, with vendor-neutral decode and error redaction
  `8f746b0` and the markets-route provider surface `aa06b3a`.
- *Files:* `corollary/data/providers/`, `corollary/wire.py`,
  `corollary/ratelimit.py`.

**4. `BrokerAccount` + `AlpacaBroker` read surface.**
- *Status:* landed `6a08db6`; recorded trading-endpoint fixtures, with
  redaction that sees into prose, `26dfa62`.
- *Files:* `corollary/engine/execution/{interface,alpaca}.py`,
  `tests/fixtures/alpaca/`.

**5. Fill ingestion, FIFO matcher, `realized_trade`.**
- *Status:* landed — the matcher `430c327`, ingestion validated against a real
  settlement `3219403`. The reconciliation identity in *Open questions 2* is the
  check to re-run whenever the ledger changes.
- *Files:* `corollary/engine/{ingest,ledger}.py`.

**6. Multi-leg grouping.**
- *Status:* landed `05032bc`.
- *Files:* `corollary/engine/grouping.py`.

**7.** API routes and schemas; frontend query migration page by page — Account, Activity, Dashboard, Markets, **and Settings**. Settings was added 2026-09-12: it sizes risk ceilings against `ACCOUNT_SNAPSHOTS` fixture equity, so its raise-confirm quotes a dollar consequence computed from $100,000 of fake money rather than the real balance. It cannot place a trade and the engine enforces the true ceiling server-side regardless, so this is a misquote rather than a hole -- but rule 4's whole point is that the number a human approves is the real one. Its endpoints already exist. **Ingestion must fetch contract terms for any symbol carrying an option event — this is now load-bearing, not optional.** Added 2026-09-11 after steps 5 and 6 landed. `contracts` was described earlier in this spec as optional and touching no money; that is no longer true. The matcher takes `multiplier` per contract and **refuses to book a P&L it cannot state correctly**, so a symbol whose terms were never fetched produces no realized trade at all when it expires or is exercised. That is the same "terminal that believes you never win" failure as a wrong `net_amount`, arriving by refusal rather than by bad arithmetic — visible rather than silent, since every refusal logs its rule, inputs and timestamp, but a gap in lifetime P&L either way.
- *Status:* landed. The HTTP layer `2713707`, the typed client and query hooks
  `8374da1`, Account `6746e7f`, then Activity/Settings/Dashboard/Markets
  `9462ff0`, with `fdf0616`, `733beab`, `b76cf05`, `fd53a40`, `ef4d7c4`,
  `fe7efb9`, `ff4e766` and `36b5497` behind them.
- *Files:* `corollary/api/routes/`, `corollary/api/schemas.py`,
  `web/src/lib/{api,queries,types}.ts`, `web/src/pages/`.

**8.** WS fan-out, `EngineRuntime`, watchdog; simplify `store.tick()`. **Also persist the matcher's refusals — decision 14.** `EngineRuntime` owns the ingest loop and therefore owns `IngestResult`, which is the cheapest point to hand a rejection's rule and inputs to the API; doing it here is what lets the Activity page name a gap's cause instead of only counting it.
- *Status:* **partially landed — six sub-steps, two of which are not written.**
  They are listed individually below rather than behind this one line, which is
  the change this rewrite exists to make.

**8a. `engine/stream.py` — the subscription budget, enforced rather than noted.**
- *Status:* landed `d8cd358`, **on a premise the spec now contradicts.** It
  rations one 30-symbol pool across both streams; decision 17 establishes that
  the 30 is the *equity* stream alone and options carry a separate 200-quote
  budget. Its logic is right and its inputs are wrong. Not a money bug — it
  under-subscribes rather than over-subscribes — but a full eight-position book
  reports position contracts as *"not streamed"* with ~170 option slots free,
  and a stale mark looks exactly like a quiet market. **Step 11 is the fix and
  is in flight.**
- *Files:* `corollary/engine/stream.py`, `tests/engine/test_stream.py`.

**8b. `engine/runtime.py` and the FastAPI lifespan.**
- *Status:* landed `977c055`, with `a049596` (a late halt record names the alert
  it belongs to) and `d3a830d` behind it. The watchdog, the halt-once rule and
  the never-auto-resume rule are all here and tested; what it does not have is a
  producer, which is 8d.
- *Carries three follow-ups and one open design question*, in *Carried items*
  below and in 8d respectively.
- *Files:* `corollary/engine/runtime.py`, `corollary/api/app.py`.

**8c-1. The `ledger_rejection` table — decision 14's persistence half.**
- *Status:* landed `1824191`, migration 0004, plus `wire.py` gaining a separate
  storage bound so a stored cause is not cut off mid-sentence while the log
  keeps `ERROR_BODY_MAX`. One scrubber, two limits — never a second redactor.
  Rows are written and **nothing serves them**; that is 8c-2.
- *Files:* `corollary/db/models.py`, `corollary/engine/ingest.py`,
  `corollary/engine/ledger.py`, `corollary/wire.py`,
  `corollary/db/migrations/versions/0004_ledger_rejection.py`.

**8e. `store.tick()` simplification.**
- *Status:* **satisfied, and deliberately not by deletion. Do not dispatch it.**
  Verified by grep rather than assumed: `store.tick()` and `store.flatten()`
  have **zero production callers** in `web/src` — only comments in
  `mockData.ts`, `queries.ts`, `Dashboard.tsx`, `Activity.tsx` and
  `CommandPalette.tsx` describing what they used to do. Step 7 moved all six
  pages onto the API, which is what the step existed to achieve: the hazard was
  a simulated fill applied to a live position, and nothing in production calls
  the simulator. The inert store survives to Phase 6 on the web agent's
  reasoning, recorded in PRD's Phase 6 section — `flatten()`'s test is where
  rule 7's halt-versus-flatten distinction is actually written down, so deleting
  the function would take the explanation with it.
- *Consequence to carry into step 14:* two fixture-era hooks are orphaned with
  it. `useMarketPoll` and `useLiveTick` have no callers either, and nothing has
  replaced their cadence.

**9. Finnhub market cap; fixture markers.**
- *Status:* landed — market cap plus regular-trading-hours series scoping
  `6d9c196`, per-page fixture markers `96d47bf`. The same commit carries the two
  series-ceiling findings now recorded under *Constraints*: the ~2.46× fetch/serve
  asymmetry that makes `MAX_RESPONSE_POINTS` a byte ceiling rather than a
  round-trip one, and `1H`'s inability to begin a bar at the open, which is why
  it is still askable and no longer suggested.
- *Files:* `corollary/data/providers/{finnhub,fundamentals}.py`,
  `corollary/api/routes/markets.py`, `corollary/api/deps.py`,
  `web/src/components/FixtureMarker.tsx`, `web/src/pages/{News,Research,Settings}.tsx`.

### Phase 2 — remaining

**8d. `api/routes/ws.py` — the websocket transport.** Fan quotes and
`trade_updates` out to the browser, and give the watchdog its producers.
- *Status:* **not written, and this is the largest remaining Phase 2 item.**
  Proven rather than inferred: `corollary/api/routes/` contains no `ws.py`;
  `api/routes/__init__.py` says *"Still to land: `ws`, at step 8"*; and every
  watchdog activity recorder — `record_message`, `record_poll`,
  `_last_activity_at`, stream open and stream close — has **zero callers outside
  `runtime.py`**. Rule 9's dead-man's switch therefore has no producer for
  either halt condition. Supervised is not armed.
- *Frame contract, decided 2026-09-13 and recorded here rather than in scratch:*
  the socket carries **quotes and `trade_updates` only**. Engine state and
  notifications are **polled at 15s**. Rule 9 halts *because* the socket closed,
  so a halt notification cannot ride the socket; and a broken client socket must
  stay distinguishable from a halted engine, or a human presses Resume on an
  engine that was never halted.
- *Open design question this step must close — it is a rule 9 question, not a
  bug fix.* Measured against `runtime.py` before 8d: the engine is running, the
  feed dies for 90s, `halt()` announces, SQLite answers *"database is locked"*,
  the feed returns within one tick, the lock clears after. Final state is
  `halted=False`, `halted_reason=None`, one critical *"Engine halted"*
  notification delivered, and **no human resume ever requested** — the engine
  traded on through a connection loss that rule 9 says must end in a halt only a
  human clears. `_retry_persist` is reachable only from the suppressed branch,
  which requires `decision is not None`, so the healthy branch is the last place
  that could repair the row and deliberately does not. An ablation confirmed 8b
  neither opened nor widened this; it is pre-existing. **Needs a ruling before
  8d lands**, because the fix is a choice about what an unpersisted halt means,
  not a defect with one correct repair.
- *Buildable here for near-nothing and required at Phase 4:* U2's
  acknowledgement reconciliation — compare `SubscriptionPlan.subscribed` against
  the server's `subscription` message per channel, count absentees into *"N
  symbols not streamed"*, and treat a `405` as an authoritative cap correction
  that lowers the effective option cap and re-plans.
- *Depends on:* step 11 for the two-plan shape it subscribes. Landing 8d first
  against today's single plan is possible and means re-pointing it afterwards.
- *Blocks:* 12 (a second writer into the quote map arrives with this step), 15
  (the viewport hint is a client message on this socket).
- *Files:* new `corollary/api/routes/ws.py`; `corollary/api/schemas.py`;
  `corollary/api/routes/__init__.py`; call sites in `corollary/engine/runtime.py`
  — **in flight at step 11, coordinate before touching it**;
  `web/src/lib/api.ts`, `web/src/lib/store.ts`.

**8c-2. A refusal's cause reaches the screen — decision 14's reporting half.**
- *Status:* **not done.** `1824191` touched no `api/routes/` and no `web/`.
  Verified: `ledger_rejection` appears nowhere under `corollary/api/`, and
  Activity's `notBooked` / `notBookedSymbols` are computed by `unbooked_closes()`
  over fills and trades — a count and a list of symbols, **not a cause**. The
  page can say *"N closings are missing from the figures above"* and cannot yet
  say why, which is the half of decision 14 that makes a known-incomplete figure
  legible instead of merely flagged.
- *Depends on:* 8c-1 (landed). Independent of 8d.
- *Files:* `corollary/api/routes/activity.py`, `corollary/api/schemas.py`,
  `web/src/lib/{api,types}.ts`, `web/src/pages/Activity.tsx`.

**11. The two stream budgets, and the mixed-unit refusal — decision 17.**
Phase 2. Split `STREAM_SYMBOL_CAP` into an equity cap of 30 and an option cap of
200, plan each stream separately, and reject a subscription unit whose symbols
would land in two different budgets.
- *Status:* **in flight right now**, in a concurrent dispatch.
  `tests/engine/test_stream.py` is modified in the working tree. **Do not edit**
  `corollary/engine/stream.py`, `corollary/engine/runtime.py`, their tests,
  `corollary/data/providers/interface.py` or
  `corollary/engine/execution/interface.py` while it runs.
- *The work, from decision 17:* two named constants with the stream in the name;
  `stream_symbol_cap_for_plan` becomes per-stream, with
  `UNLIMITED_STREAM_SYMBOL_CAP` reaching equities only and a **real 1000** on
  options; `plan_stream_subscriptions` returns two plans and the caller
  subscribes each to its own socket; *"N symbols not streamed"* sums across both
  into one number; and a unit carrying both an OCC symbol and an equity ticker
  raises as a caller bug, because all-or-nothing cannot survive a unit split
  across two budgets.
- *Depends on:* nothing. It corrects 8a.
- *Blocks:* 15, and the shape 8d subscribes.
- *Files:* as listed above.

**12. `UnderlyingQuote` provenance and the field-level merge — decision 18.**
Phase 2. Make one quote map safe for two writers.
- *Status:* not started. Verified: `UnderlyingQuote` in `web/src/lib/types.ts`
  carries neither `at` nor `source`, and `change` / `changePct` are **stored on
  the wire** today rather than derived.
- *The work, from decision 18:* `at` is the **vendor's** observation timestamp,
  never the client clock; `source` is `'stream' | 'poll'`; price is
  last-observation-wins on `at` with the stream winning a tie; the merge is
  field-level, so a stream write never blanks `previousClose`, session volume,
  average volume or market cap and a poll write never stomps a fresher streamed
  price; `change` and `changePct` are derived in a selector at read time;
  `lastTickAt` and `lastPollAt` stay separate, because *"is the stream alive"*
  and *"is the poll alive"* are different questions and collapsing them lets a
  healthy poll hide a dead socket.
- *Why it sits before the cadence work rather than after it:* the second writer
  arrives with **8d**, not with step 15 — position underlyings are equity
  symbols that the Markets table also polls. Landing the merge rules afterwards
  means shipping the screen-flickers-backwards-in-time bug first and fixing it
  second, on the one map CLAUDE.md spends a paragraph protecting.
- *Depends on:* 8d for the stream half; the poll half stands alone.
- *Blocks:* 15.
- *Files:* `corollary/api/schemas.py`, `corollary/api/routes/markets.py`,
  `web/src/lib/{types,api,store}.ts`.

**13. The `/api/markets/stocks` coalescing cache — decision 18.** Phase 2.
Concurrent and near-simultaneous callers share one in-flight Alpaca request,
keyed on the requested symbol set, TTL equal to the foreground interval.
- *Status:* not started — there is no coalescing in
  `corollary/api/routes/markets.py`.
- *Why it must land before the interval drops:* the browser drives the poll and
  the API forwards it, so two tabs, a reload loop or a hot-reloading dev server
  multiply the Alpaca rate by the number of clients — 400ms × 2 clients is
  300/min against a 200/min ceiling. `ratelimit.py`'s bucket **waits** rather
  than refusing, so the symptom is not an error: every Markets request simply
  gets slower until the page looks broken for a reason nothing logs. The bucket
  is the hard backstop; the cache is what keeps it from ever being reached.
  Dropping the interval first ships exactly that failure.
- *Depends on:* nothing.
- *Blocks:* 14.
- *Files:* `corollary/api/routes/markets.py`, `corollary/api/deps.py`.

**14. The three-state cadence hook — decision 18.** Phase 2. Hidden, foreground
and background are three states, not two.
- *Status:* not started, and the starting point is further back than it looks:
  **nothing refreshes Markets at all today.** There is no `refetchInterval`
  anywhere in `web/src`, and `useMarketPoll` — which drives the *fixture* store's
  `pollMarkets` — has had no callers since step 7 moved the pages onto the API.
  `useLiveTick` is orphaned the same way. This is the reintroduction of live
  refresh against a real feed, not an adjustment to a working cadence; read the
  old hook as a fixture artefact rather than as the thing being retuned.
- *The work, from decision 18:* **hidden** — `document.hidden` true, the poll
  stops entirely and the first poll on return is immediate; **foreground** —
  visible **and the Markets route mounted**, 400ms; **background** — visible,
  Markets not mounted, 5s from one app-level hook so exactly one interval exists
  at a time, keeping the server's session-volume and daily-series caches warm.
  Which page is open is read from the router, never from a store flag: a flag
  set on navigation survives a crash, a modal, or a route the author forgot.
  400ms is 150/min of a 200/min bucket, leaving ~49/min for on-demand chains;
  300ms is exactly at the ceiling with nothing left, which is why the floor is
  400 and not 300.
- *Depends on:* 13 — the server must bound the rate before the client is
  allowed to ask for it faster.
- *Files:* `web/src/hooks/useMarketPoll.ts` (rewrite or replace),
  `web/src/lib/queries.ts`, `web/src/pages/Markets.tsx`, `web/src/App.tsx`.

**15. The `MARKETS_VISIBLE` tier and the viewport hint — decision 18.** Phase 2.
Spend the ~22 equity slots left after position underlyings on the Markets rows
actually on screen.
- *Status:* not started — `MARKETS_VISIBLE` appears nowhere in the tree.
- *The work, from decision 18:* a **new lowest** `SubscriptionPriority`, fed by a
  debounced client message on the existing WS that names the visible rows and is
  sent only when the set actually differs. The server treats it as input to that
  tier and nothing else: a client-supplied list can **never** outrank a position
  contract or a position underlying, because a client that could evict a held
  contract from the stream could make a position mark stale by scrolling. That
  is rule 4's principle applied to a stream budget. Churn is harmless by
  construction — every Markets row is polled anyway, so losing a slot costs
  freshness, never a price.
- *Depends on:* 11 (the equity budget and the two-plan shape), 8d (the
  client-to-server message), 12 (it is the second writer).
- *Files:* `corollary/engine/stream.py`, `corollary/engine/runtime.py`,
  `corollary/api/routes/ws.py`, `web/src/pages/Markets.tsx`.

**10. Doc amendments.** Phase 2, **last**, so they describe what was actually
built.
- *Status:* partially applied. PRD's five amendments landed 2026-09-11
  (`53d5097`, `3f8ca60`); PRD §12 and the two-budget / Phase 4 corrections
  landed in `073c9a6`, which also amended CLAUDE.md's cap sentences.
- *Outstanding — CLAUDE.md's layout and vendor-surface amendment:* the vendor
  surface gains `BrokerAccount`; the layout gains
  `engine/{ledger,grouping,runtime,stream}.py`, `api/routes/`, `api/schemas.py`
  and `corollary/wire.py`; `CONTRACT_MULTIPLIER` is named as a **fixture**
  default, since it lives in `web/src/lib/mockData.ts` and nothing under
  `corollary/` defines it; and the options level is read from the account rather
  than hardcoded to 3. The content is factual and relaxes no rule, but **it needs
  the repo owner's own go-ahead** — no agent message authorizes a CLAUDE.md
  edit, and a spec plus a prior agent's note is not the owner's word.

### Carried items that are not steps — Phase 2 unless stated

Findings that were live only in `.claude/scratch/` and would have been lost with
it. None blocks a step; each is small, and each is here because the reasoning
behind it is more expensive to rebuild than to record.

- **`_write_trades`'s canonical-symbol premise is unpinned.** `ledger.py` skips
  deleting a `realized_trade` row when its symbol is in `guarded`, which is safe
  only because `RealizedTrade.symbol` is canonical on every writer path. That is
  true today and nothing proves it: changing either `LotMovement(...)`
  constructor from `symbol=contract.symbol` to `symbol=activity.symbol` would
  leave the whole suite green and reinstate the money-row deletion chain an
  earlier audit found — a booked trade vanishing out of lifetime P&L. One test
  closes it: drive a lower-cased or padded vendor symbol through `build_ledger`
  and assert the resulting record is canonical. Hardening, not a defect; worth
  doing before Phase 3 touches the matcher.
- **Three test files still outside the `-m risk` gate.** The marker now collects
  64 tests rather than 1 (`04e0864`), but `tests/api/test_account_mode.py`
  (rules 1 and 5 — including the structural order-verb and vendor-SDK guards
  applied to the API package, plus cold-start-in-Paper), `tests/db/test_risk_limit_bounds.py`
  (rule 4 — `Decimal('Inf')`, NaN and a negative ceiling each round-tripped
  before that file existed, and each disables the risk manager outright) and
  `tests/db/test_money_sql.py` carry no `@pytest.mark.risk` at all. The gate that
  runs before every engine change does not currently see them.
- **`engine_state` and `mark_started` are persistence living in
  `api/routes/engine.py`.** `EngineRuntime` imports them inside method bodies to
  dodge a circular import and documents why. The right fix is to move both out
  of the route module; the alternative — a second copy of the t₀ rule and the
  create-if-missing rule inside `engine/` — is exactly the duplication rule 9 is
  most vulnerable to. Do it before more code imports them lazily.
- **`check_watchdog`'s four-state docstring table is no longer exact.** After the
  fault-ends clearing rule, `_announced is None` has a third meaning the table
  does not enumerate — announced, never recorded, episode ended, fault returned.
  A reader debugging *"why did it announce twice in one outage?"* reasons from
  the table and hunts a resume that never happened.
- **Two sub-bar accuracy notes in the ingest path.** `ingest.py`'s `guarded`
  comment under-counts its own inputs — `NOT_AN_OPTION` and
  `NOT_A_LEDGER_ACTIVITY` also reject before a contract is resolved and also pass
  `activity.symbol` verbatim — and `MISSING_FEE_AMOUNT` in `ledger.py` is a fifth
  such rule, complete in practice only because Alpaca's `FEE` rows carry no
  symbol.
- **The commit trailer on this branch is not the one recent dispatches ask for.**
  Every commit here carries `Co-Authored-By: Claude Sonnet 5`; recent dispatch
  context names `Claude Opus 5 (1M context)`. Amending history is a human call
  and not one to take on inference.
- **Two web findings already logged against Phase 4** by the page migration, in
  PRD's Phase 6 section: `Research.tsx` reads `isHalted` from the Zustand fixture
  store while Dashboard, the command palette and both order tickets read
  `GET /api/engine/state`, so Research can show a halted engine as running —
  bounded while its Execute button is fixture-backed, unbounded the moment it
  submits. And three surfaces claim *"Submits to the risk manager"* while none
  does; consistent rather than a silent no-op, but true only once
  `RiskManager.approve()` exists.

### Phase 4 — the Algo Trader Plus upgrade (decision 20)

Lettered rather than numbered so that renumbering the Phase 2 list above
cannot collide with it, and listed here rather than in a separate document so
the upgrade is a checklist instead of a redesign. **None of this is Phase 2 work.** Order matters within
the group: U1 and U2 are what make U3 safe.

- **U1. The plan lookup returns a pair.** `corollary/engine/runtime.py` —
  `stream_symbol_cap_for_plan` and `EngineRuntime.stream_symbol_cap` become one
  `stream_budget_for_plan(plan) -> StreamBudget(equity, option)`, constructed
  nowhere else. `tests/engine/test_runtime.py` gains the assertion that
  `UNLIMITED_STREAM_SYMBOL_CAP` never reaches `.option` on any plan and that
  `.option == 1000` on the paid one. Decision 20.4.
- **U2. Acknowledgement reconciliation on all three sockets.** The WS
  transport (Phase 2 step 8) compares `SubscriptionPlan.subscribed` against the
  server's `subscription` message per channel, counts any absentee into *"N
  symbols not streamed"*, and treats a `405` as a cap correction that lowers
  the effective option cap and re-plans. Buildable at step 8 for near-nothing;
  required by Phase 4. Decision 20.4.
- **U3. Flip the four environment variables and restart.**
  `ALPACA_OPTIONS_FEED=opra`, `ALPACA_STOCK_FEED_REALTIME=sip`,
  `ALPACA_STOCK_FEED_HISTORICAL=sip` (unchanged), `ALPACA_DATA_PLAN=algo_trader_plus`.
  Add nothing to `.env.example` — all four names are already there. The engine
  comes up halted and in Paper; resume by hand after the acknowledgements match.
  Decision 20.7.
- **U4. The rate-limit bucket becomes plan-derived per host.**
  `corollary/ratelimit.py` — `data.alpaca.markets` to 10,000/min on the paid
  plan, `paper-api.alpaca.markets` **left at 200/min**. The asymmetry is the
  whole point of the change. Decision 20.1.
- **U5. The sweep replaces the poll.** `web/src/hooks/useMarketPoll.ts` and its
  two constants — foreground 400ms → 5s, background 5s → 30s, hidden unchanged.
  The server-side coalescing cache on `GET /api/markets/stocks` keeps its
  TTL-follows-the-interval rule and gets its rationale rewritten. Add the
  post-reconnect snapshot re-read. Decision 20.1.
- **U6. The equity stream takes every Markets symbol**, and the viewport hint's
  equity binding is deleted — client message, debounce, and the equity producer
  for `MARKETS_VISIBLE`. Decision 20.1, 20.6.
- **U7. The chain tier.** `MARKETS_VISIBLE` is re-pointed at option contracts:
  one unit per visible chain row, in display order, fed by the viewport hint,
  planned against the 1000-quote budget behind position contracts and
  recommendations. Bound the recommendation tier at the endpoint that serves it.
  Decision 20.2.
- **U8. The vestigial sweep.** Delete, rewrite or keep per decision 20.6's
  three lists — `web/src/components/LiveStatus.tsx` (lines 42 and 81),
  `web/src/pages/Markets.tsx:869`, `web/src/lib/settings.ts`'s `feedWarning`,
  `MAX_RESPONSE_POINTS`'s round-trip paragraph in
  `corollary/api/routes/markets.py`, and the equity half of the *"30 symbols"*
  sentences in `engine/stream.py`, `engine/runtime.py`,
  `data/providers/interface.py`, `engine/execution/interface.py`, three hooks
  under `web/src/hooks/`, `web/src/lib/store.ts` and `web/src/lib/mockData.ts`.
- **U9. Measure the two things that are only measurable then** — inbound
  message rate at the full subscription set, and OPRA's IV/greeks coverage
  against decision 10's 19-of-100 — and revisit `_fetch_session_volumes` and
  `UnderlyingChart`'s appended live point now that the embargo is gone.
  Decisions 20.5, 20.7.
- **U10. Doc amendments** — PRD §7's stream-budget paragraph and CLAUDE.md's
  *"changes nothing else"* sentence, per *Doc amendments* above. Last, so they
  describe what was actually built.

---

## Out of scope

No `submit_order`, no `RiskManager.approve()` body, no `BrokerExecution`. No `SimBroker`, Parquet, DuckDB or backtest worker. No news, sentiment, calendar, consensus or social data. No strategies, scanner, LLM layer or indicator whitelist. No rolling, trailing stops, or greeks in the ticket — all three already deferred by §8.2.
