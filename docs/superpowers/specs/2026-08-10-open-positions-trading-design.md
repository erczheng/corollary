# Open Positions: charts, order types, and position management

**Date:** 2026-08-10
**Status:** Approved, not yet implemented
**Scope:** The Open Positions table on the Activity page (`web/src/pages/Activity.tsx`)
**Phase:** 1 — mock data. No order reaches a broker.

---

## Problem

Open Positions currently renders seven read-only columns and a single **Close** action that submits a market order at the touch. That is the only thing you can do to a position from inside Corollary, which leaves three gaps:

1. No way to see how a position got where it is, or what it does from here.
2. No way to exit on your own terms — limit, stop, or stop-limit.
3. No way to add to a position you already hold.

## Constraints that shaped the design

These were verified against Alpaca's documentation rather than assumed. Getting any of them wrong produces a control that the broker rejects at submit time in Phase 2.

**Order types on options.** Single-leg option orders accept Market, Limit, Stop and Stop-Limit. Time-in-force is restricted to **DAY and GTC only** — IOC, FOK, OPG and CLS are all rejected for options.

**Multi-leg is narrower.** Every multi-leg (`order_class: mleg`) example Alpaca publishes uses `type: limit`. Level 3 additionally rejects an MLeg order whose legs are not all covered within the same order. A market order on a spread is also a bad idea independent of what the API permits. Multi-leg positions therefore offer **Limit only**.

**OCO is the right primitive for an attached exit.** Alpaca documents `order_class: oco` as "the second part of the bracket orders where the entry order is already filled… you can add take-profit and stop-loss after you open the position." A *bracket* bundles an entry order, which is wrong here — the position already exists.

- The take-profit leg must be `type: limit`.
- The stop leg takes a mandatory `stop_price` and an optional `limit_price`; supplying the latter makes it a stop-limit.
- The stop price must be at least **$0.01 below** the take-profit limit *and* below the current mark, for a sell-side exit. Above, for a buy-side one.
- Order replacement is supported for OCO. This design does not use it yet — see Out of scope.

**Unverified:** whether `order_class: oco` is accepted for *options* specifically. Every documented example is equity SPY, and the options support matrix covers order *types* but not order *classes*. This is resolved by the fallback in "Where the exit is held" below.

**Positions already have engine-managed exits.** PRD §5.1 gives every strategy `exits: {profit_target_pct, stop_loss_pct, time_stop_dte}`, and CLAUDE.md rule 7 says those keep running through a halt. Manual order entry has to say what happens to them.

**Historical option data is bars only.** Alpaca serves no historical option quotes at all, ever. A contract price series is available back to Feb 2024; a bid/ask-spread history is not. The value chart is therefore derived from bars.

---

## Decisions

### Manual exit replaces managed exit

Placing a manual exit **detaches** the position from its strategy's exit rules. The engine stops managing it and the manual order becomes the only exit.

The alternative — both exits live, first to trigger wins — was rejected. Two exits on one position double-close when the cancel races the fill, and the failure is silent and expensive. Detaching keeps "who is responsible for closing this position" a single question with an always-available answer, which is the property that matters at the moment something goes wrong.

Detach is visible on the row, and reversible.

### Where the exit is held

Attached exits are submitted to the broker as OCO where possible, and fall back to Corollary-managed where not. **The row states which**, because the difference is whether the exit survives Corollary being down:

- `exit held at broker` — fires whether or not the engine is running.
- `exit held by Corollary` — the engine watches the position and fires a close through `RiskManager.approve()`. Does not exist while the engine is down.

In Phase 1 both render identically from fixtures. The label is designed in now because retrofitting it after Phase 2 reveals which one we get would mean changing the row, the store and the tests at once.

### Add, not Buy

The action is **Add to position**, never "Buy more". On a short credit spread, adding is a *sell to open*. A button reading "Buy" would name the opposite of the order it places. The ticket spells out the resolved side (BTO / STO) underneath.

---

## Design

### Interaction

The row's current **Close** button is **replaced**, not supplemented. Close becomes one item among several, and leaving a bare Close beside a menu that also contains Close would give the same action two spellings with different behaviour — today's button submits at market, the menu item opens a ticket where market is one choice of four.

Each position row gains:

- a **chevron** that expands the row in place, and
- a **⋯ menu** with `Close position`, `Add to position`, `Attach exit`, `Detach from strategy`. Selecting an item expands the row *and* opens that ticket mode.

The existing `closePosition(id)` store action and its confirm dialog are subsumed by `submitPositionOrder(id, draft)` with mode `close` and type `market`. Its behaviour is preserved — including that closing one position never halts the engine — and its existing tests are rewritten against the new action rather than deleted.

One row expands at a time. With 2–4 positions per account an accordion keeps the page short and makes the target of an action unambiguous. Nothing is covered by a modal — the surrounding context, especially which account is live, stays visible while sizing a trade.

The expanded panel spans the table width:

```
┌─ status line: who manages this position's exit ──────────────────┐
├───────────────────────────────┬──────────────────────────────────┤
│  [Value since entry] [Payoff] │  [Close] [Add] [Attach exit]     │
│                               │                                  │
│         chart                 │         order ticket             │
└───────────────────────────────┴──────────────────────────────────┘
```

The status line reads either `Managed by spx_mean_reversion — target 50%, stop 200%, 2 DTE` or `Manually managed — exit held at broker`.

### Chart

A segmented toggle over one chart area.

**Value since entry** (default) plots position value over time with cost basis as a horizontal reference line, so unrealized P&L is the gap between the series and the reference. It plots *position value*, not contract price, because contract price inverts for a short and would read backwards on the SPY credit spread.

**Payoff at expiry** plots profit against underlying price, with breakeven and the current underlying marked. Computed from strikes, premium and multiplier — it needs no market data, so it is genuinely real in Phase 1 rather than mock-shaped.

Note which field is which on `Position`: `last` is the **underlying's** price (232.40 on the AAPL position), while `bid`/`ask` are the **contract's** (2.04 / 2.08). The payoff curve's "current underlying" marker reads `last`; the ticket's price defaults read `bid`/`ask`. Crossing these produces a chart that is wrong by two orders of magnitude and still looks plausible.

### Ticket

Three modes. Fields common to Close and Add:

| Field | Notes |
|---|---|
| Quantity | Defaults to full position on Close, 1 on Add. Capped at position size on Close. |
| Order type | Derived from the position — see table below. |
| Limit price | Shown for Limit and Stop-Limit. Defaults to mid; bid and ask displayed beside it. |
| Stop price | Shown for Stop and Stop-Limit. |
| Time in force | Day or GTC. Those are the only two options accepts. |
| Resolved side | `Sell to close (STC)` / `Buy to close (BTC)` / `Buy to open (BTO)` / `Sell to open (STO)`. |
| Estimate | Proceeds for a credit, cost for a debit. Never both under one word. |

Order types available:

| Position | Market | Limit | Stop | Stop-Limit |
|---|---|---|---|---|
| Single-leg | yes | yes | yes | yes |
| Multi-leg | no | yes | no | no |

The ticket states why a multi-leg position is restricted rather than silently offering less.

**Attach exit** mode takes a take-profit limit price and a stop price, with an optional stop limit price. It validates the $0.01 threshold against both the take-profit and the current mark, and names where the exit will be held.

Every submit routes through the existing `ConfirmDialog`, which states the consequence in concrete terms — contracts, side, prices, and the resulting estimate.

### Risk limits are advisory here

**Add to position** can breach `max_exposure_per_underlying` or `max_concurrent_positions`. The ticket shows an estimate against those ceilings, explicitly labelled as an estimate. It never renders an approval and never blocks on its own arithmetic: CLAUDE.md rule 4 says the engine enforces limits and a value from the client is never trusted. The UI's job is to make a rejection unsurprising, not to pre-empt it.

---

## Code layout

`Activity.tsx` is ~260 lines and would roughly double. Extracting, in the direction the existing structure already points:

| File | Responsibility |
|---|---|
| `web/src/components/PositionRow.tsx` | One row, its ⋯ menu, and its expansion |
| `web/src/components/OrderTicket.tsx` | The three ticket modes and their fields |
| `web/src/components/PositionChart.tsx` | Value and payoff charts (Recharts) |
| `web/src/lib/orders.ts` | Order-type availability, price validation, estimate math, payoff curve |
| `web/src/pages/Activity.tsx` | Composition only |

`lib/orders.ts` holds every rule that could be wrong about money, as pure functions with no React in them, so they are unit-testable without rendering.

### Data model

Added to `web/src/lib/mockData.ts`:

```ts
type OrderType = 'market' | 'limit' | 'stop' | 'stop_limit'
type TimeInForce = 'day' | 'gtc'
type ExitHolder = 'broker' | 'corollary'

interface PositionLeg {
  symbol: string          // OCC format
  strike: number
  right: 'call' | 'put'
  side: 'long' | 'short'
  ratio: number
}

interface AttachedExit {
  takeProfit: number
  stopPrice: number
  stopLimitPrice: number | null
  timeInForce: TimeInForce
  heldBy: ExitHolder
}

interface ManagedExit {
  profitTargetPct: number
  stopLossPct: number
  timeStopDte: number
}

// Position gains:
  legs: PositionLeg[]           // length > 1 ⇒ multi-leg ⇒ limit only
  strategyId: string | null     // null once detached
  managedExit: ManagedExit | null
  attachedExit: AttachedExit | null
  valueHistory: PricePoint[]
```

Store actions added to `web/src/lib/store.ts`, all account-scoped like the existing ones:

- `submitPositionOrder(positionId, draft)` — Close or Add
- `attachExit(positionId, exit)` / `cancelAttachedExit(positionId)`
- `detachFromStrategy(positionId)` / `reattachToStrategy(positionId)`

Every one appends to the account's activity feed exactly as a broker fill would, so the ledger stays the single record of what happened.

### Fixture invariant

`valueHistory` **must start at the position's `costBasis` and end at its current `value`.** A chart that disagrees with the row above it is worse than no chart. This is generated deterministically from the existing seeded PRNG and asserted in `mockData.test.ts`.

---

## Testing

**Unit — `lib/orders.ts`:**

- Order types available for a single-leg vs a multi-leg position.
- Stop-price threshold: accepts $0.01 below, rejects equal and above; both sell-side and buy-side.
- Estimate math for a long close (bid) and a short close (ask), including the ×100 multiplier.
- Resolved side: Add on a long is BTO, Add on a short is STO.
- Payoff curve endpoints for a long call and a defined-risk spread — max loss is premium paid and width-minus-credit respectively.

**Component:**

- Row expands and collapses; only one open at a time.
- ⋯ menu items open the right ticket mode.
- The spread's type select offers Limit alone; the single-leg's offers four.
- Confirm dialog wording differs correctly for long vs short, close vs add.
- Detaching flips the status line and stops showing the strategy's exits.
- Chart toggle switches series without unmounting the panel.

**Fixture:** the `valueHistory` invariant above.

---

## Out of scope

- Rolling a position (Alpaca rejects a roll of a short spread as an uncovered MLeg leg).
- Trailing stops — not in the options order-type matrix.
- Greeks and IV in the ticket.
- Editing an attached exit in place. OCO supports replacement; this design cancels and re-places. Worth revisiting once Phase 2 confirms OCO works for options at all.
- Any real order path. Phase 1 mutates the store. Phase 2 routes through `RiskManager.approve()` and nowhere else.
