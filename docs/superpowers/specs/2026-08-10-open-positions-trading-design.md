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
- Order replacement is supported for OCO, updating `limit_price` and `stop_price`. This design uses it: an attached exit is **edited in place**, never cancelled and re-placed.

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

### One closing order per position, edited rather than replaced

An attached exit is **edited in place** — OCO supports replacement, so changing a take-profit or a stop updates the existing order rather than cancelling it and submitting a new one. The gap between a cancel and its replacement is a window with no exit on the position at all, and it is exactly the window a fast market runs through.

**A new closing order overrides the existing one.** Submitting a close, or attaching an exit where one already exists, replaces it. There is never more than one live closing order on a position.

This is the same principle as the detach decision above, applied to a different pair: two live exits on one position double-close when a cancel races a fill, and the failure is silent. One position, one closing order, always answerable.

### Add, not Buy

The action is **Add to position**, never "Buy more". On a short credit spread, adding is a *sell to open*. A button reading "Buy" would name the opposite of the order it places. The ticket spells out the resolved side (BTO / STO) underneath.

---

## Design

### Interaction

The row keeps its **Close** button, but Close becomes *the order button*: it expands the row and opens the ticket in `close` mode, defaulting to a market order. It no longer submits on click.

That keeps one spelling for closing a position. The alternative — a bare Close that fires at market plus a menu item that opens a ticket — gives the same word two behaviours on the same row, and the fast one is the irreversible one. Close therefore does **not** appear in the ⋯ menu; the button is the only way in, and the ticket's default is the market order the old button used to submit, one confirm further along.

So each row carries:

- **Close** — expands and opens the ticket in `close` mode, type `market`
- **⋯ menu** — `Add to position`, `Attach exit` (or `Edit exit`, where one exists), `Detach from strategy`; each expands the row and opens that mode
- **chevron** — expands to the default view (chart, no ticket mode preselected)

The existing `closePosition(id)` store action is subsumed by `submitPositionOrder(id, draft)`. Its behaviour is preserved — including that closing one position never halts the engine — and its existing tests are rewritten against the new action rather than deleted.

One row expands at a time. With 2–4 positions per account an accordion keeps the page short and makes the target of an action unambiguous. Nothing is covered by a modal — the surrounding context, especially which account is live, stays visible while sizing a trade.

The expanded panel spans the table width:

```
┌─ status line: who manages this position's exit ──────────────────┐
├───────────────────────────────┬──────────────────────────────────┤
│  [Value since entry] [Payoff] │  [Close] [Add] [Exit]            │
│                               │                                  │
│         chart                 │         order ticket             │
└───────────────────────────────┴──────────────────────────────────┘
```

The status line reads either `Managed by spx_mean_reversion — target 50%, stop 200%, 2 DTE` or `Manually managed — exit held at broker`.

### Chart

A segmented toggle over one chart area.

**Value since entry** (default) plots position value over time with cost basis as a horizontal reference line, so unrealized P&L is the gap between the series and the reference. It plots *position value*, not contract price, because contract price inverts for a short and would read backwards on the SPY credit spread.

**Payoff at expiry** plots profit against underlying price, with breakeven and the current underlying marked. Computed from strikes, premium and multiplier — it needs no market data, so it is genuinely real in Phase 1 rather than mock-shaped.

**`Position.last` changes meaning as part of this work: it becomes the contract's last traded price, not the underlying's.** It previously held the underlying (232.40 on the AAPL call) while `bid`/`ask` beside it held the contract (2.04 / 2.08) — one row, three price columns, two different instruments, nothing in the names to say so. After this change `last` sits between `bid` and `ask` the way a reader already assumes it does, and the Open Positions table's Last column finally shows what the position is marked at.

The payoff curve still needs the underlying, so `Position` gains an explicit **`underlying`** field carrying that price. Two prices, each named for its own instrument, and the crossing error becomes impossible rather than merely documented.

Existing fixture values move accordingly: `last` takes a value inside the contract's spread, and `underlying` takes the price `last` used to hold.

### Ticket

Three modes. Fields common to Close and Add:

| Field | Notes |
|---|---|
| Quantity | Defaults to full position on Close, 1 on Add. Capped at position size on Close. |
| Order type | Derived from the position — see table below. |
| Limit price | Shown for Limit and Stop-Limit. Defaults to mid; bid and ask displayed beside it. |
| Stop price | Shown for Stop and Stop-Limit. |
| Time in force | Day or GTC — the only two Alpaca accepts on an option order. |
| Resolved side | `Sell to close (STC)` / `Buy to close (BTC)` / `Buy to open (BTO)` / `Sell to open (STO)`. |
| Estimate | Proceeds for a credit, cost for a debit. Never both under one word. |

Order types available:

| Position | Market | Limit | Stop | Stop-Limit |
|---|---|---|---|---|
| Single-leg | yes | yes | yes | yes |
| Multi-leg | no | yes | no | no |

The ticket states why a multi-leg position is restricted rather than silently offering less.

**Exit** mode takes a take-profit limit price and a stop price, with an optional stop limit price. It validates the $0.01 threshold against both the take-profit and the current mark, and names where the exit will be held.

The mode is one control in two states, not two modes. Where no exit is attached it reads **Attach exit** and submits a new one; where an exit already exists it reads **Edit exit**, prefills the current values, and submits a replacement in place. The ⋯ menu item changes label the same way. A separate "edit" path would be a second way to write the same field, and the whole point of the upsert decision is that there is only one.

Removing an exit entirely is a distinct action — `Cancel exit`, offered inside the mode when one exists — because deleting your only protection should not be reachable by clearing a text field.

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
  underlying: number            // the underlying's price

// Position changes meaning:
  last: number                  // now the CONTRACT's last price, was the underlying's
```

Store actions added to `web/src/lib/store.ts`, all account-scoped like the existing ones:

- `submitPositionOrder(positionId, draft)` — Close or Add. A close draft replaces any existing closing order on that position.
- `upsertExit(positionId, exit)` — attaches, or edits in place if one exists. Not `attachExit` + `cancelAttachedExit`: naming it upsert is what stops a caller from doing cancel-then-attach and reopening the unprotected window.
- `cancelExit(positionId)` — removes the attached exit outright, a deliberate act rather than half of an edit.
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
- **Close opens the ticket rather than submitting.** Clicking Close leaves the position open and the book unchanged; only confirming the ticket closes it.

**Store — the one-closing-order invariant:**

- `upsertExit` on a position that already has one edits it; the position still holds exactly one `attachedExit`.
- Submitting a close over an existing attached exit replaces it, and never leaves two.
- `cancelExit` removes it; `upsertExit` afterwards attaches a fresh one.

**Fixture:** the `valueHistory` invariant above, plus `last` sitting within `[bid, ask]` on every position — the assertion that would have caught the old underlying-in-`last` confusion.

---

## Out of scope

- Rolling a position (Alpaca rejects a roll of a short spread as an uncovered MLeg leg).
- Trailing stops — not in the options order-type matrix.
- Greeks and IV in the ticket.
- Any real order path. Phase 1 mutates the store. Phase 2 routes through `RiskManager.approve()` and nowhere else.
