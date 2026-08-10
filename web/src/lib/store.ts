import { create } from 'zustand'
import {
  ACCOUNT_SNAPSHOTS,
  CONTRACT_MULTIPLIER,
  mulberry32,
  type AccountMode,
  type ActivityItem,
  type AttachedExit,
  type ManagedExit,
  type Position,
  type PricePoint,
  type WorkingOrder,
} from './mockData'
import {
  estimate,
  exitTrigger,
  isWorkingOrderType,
  orderWouldFill,
  type OrderDraft,
} from './orders'

export type Theme = 'light' | 'dark'
export type ExecutionMode = 'manual' | 'auto'
export type { AccountMode }

interface UIState {
  theme: Theme
  /** Which Alpaca account keys are in use. Paper is the only mode that may
   * come up on a cold start (CLAUDE.md rule 5) — this store is never
   * persisted to storage, so a reload always resets here. */
  accountMode: AccountMode
  /** Whether the engine may place orders unattended. */
  executionMode: ExecutionMode
  paletteOpen: boolean
  /** Which strategy is active — only one at a time in v1 (PRD.md §5.2). */
  activeStrategyId: string
  /** Halt stops new entries only; existing positions keep their managed
   * exits. This is UI-side display state for Phase 1 mock data — the real
   * halt is enforced by the engine, never trusted from the client
   * (CLAUDE.md rule 4). */
  isHalted: boolean
  /** Positions and activity live here for Phase 1 only, so that Flatten and
   * Close have something real to act on. Both are server state and move to
   * TanStack Query in Phase 2 — do not grow this into a client-side
   * position ledger.
   *
   * Keyed by account: Paper and Cash are different accounts holding
   * different money, and only one account's keys are in use at a time. Read
   * these as `openPositions[accountMode]`, never as a merged book. */
  openPositions: Record<AccountMode, Position[]>
  activity: Record<AccountMode, ActivityItem[]>
  /** Orders that have been placed and haven't filled. Only non-market
   * orders land here — a market order fills immediately. Attached exits
   * are not duplicated in here; they live on the position. */
  workingOrders: Record<AccountMode, WorkingOrder[]>
  toggleTheme: () => void
  setAccountMode: (mode: AccountMode) => void
  setExecutionMode: (mode: ExecutionMode) => void
  setActiveStrategyId: (id: string) => void
  openPalette: () => void
  closePalette: () => void
  /** Halt and Flatten are distinct actions and never merged (CLAUDE.md
   * rule 7). Halt stops new entries and leaves every open position alone.
   * Flatten closes them all, then halts. If these two ever have the same
   * body again, one of them is wrong. */
  halt: () => void
  resume: () => void
  flatten: () => void
  /** Close or add to one position — the per-row ticket on Activity
   * (PRD.md §8.2). Distinct from Flatten in the same way Flatten is
   * distinct from Halt: acting on a single position is not a book-wide
   * event and must never halt the engine.
   *
   * A close of the full quantity removes the position; a partial close
   * scales what remains. Either way it clears any attached exit, so a
   * position never carries a closing order that no longer describes it. */
  submitPositionOrder: (id: string, draft: OrderDraft) => void
  /** Attaches a manual exit, or edits the existing one in place.
   *
   * Not `attachExit` plus `cancelExit`: the gap between a cancel and its
   * replacement is a window with no exit on the position at all, and the
   * name is what stops a caller from opening that window by hand.
   *
   * Attaching also **detaches the position from its strategy**. Two exit
   * regimes on one position double-close when a cancel races a fill, so
   * there is only ever one party responsible for closing a position. */
  upsertExit: (id: string, exit: AttachedExit) => void
  /** Removes an attached exit outright — a deliberate act, not half of an
   * edit. The position stays detached; reattaching is its own action. */
  cancelExit: (id: string) => void
  detachFromStrategy: (id: string) => void
  /** Hands the position back to **the strategy that opened it**, which
   * means dropping any manual exit — the strategy's own rules resume, and
   * both cannot run. Deliberately takes no strategy argument: reattaching
   * to whichever strategy happens to be active now would silently move the
   * position to a different set of exit rules than it was opened under. */
  reattachToStrategy: (id: string) => void
  /** Cancels a working order and flips its pending row in the ledger to
   * `canceled`, rather than appending a second row — the order had one
   * life and the feed should show it once. */
  cancelWorkingOrder: (id: string) => void
  /** When the last price update arrived, or null before the first one. The
   * header reads this to say whether the page is actually live rather than
   * merely claiming to be. */
  lastTickAt: string | null
  /** One price update.
   *
   * Stands in for the Alpaca WebSocket, which Phase 2 puts in its place.
   * Only the active account ticks: `accountMode` is which keys are in use,
   * so the other book has no stream behind it. CLAUDE.md also caps the
   * stream at 30 symbols on the Basic plan and scopes it to open
   * positions — which is what this does, one symbol per position.
   *
   * This is the mock **broker**, not the risk manager. Fills and exit
   * triggers are simulated here because there is no market to get them
   * from; nothing in this function decides whether an order is *allowed*.
   * That stays with `RiskManager.approve()` in Phase 2. */
  tick: () => void
}

/** A long is sold to close, a short is bought to close — and each crosses
 * the spread in the opposite direction.
 *
 * `idPrefix` records *why* the position closed: `act-flat-` for a book-wide
 * Flatten, `act-close-` for a single row closed from Activity. Same fill,
 * different decision, and the audit trail should be able to tell them
 * apart. */
function closeExecution(position: Position, at: string, idPrefix: string): ActivityItem {
  const long = position.direction === 'long'
  return {
    id: `${idPrefix}${position.id}`,
    time: at,
    contract: `${position.symbol} ${position.contract}`,
    action: long ? 'STC' : 'BTC',
    price: long ? position.bid : position.ask,
    quantity: position.quantity,
    pnl: position.pnl,
    pnlPct: position.pnlPct,
    amount: null, // a close reports P&L, not a cash movement
    status: 'filled',
  }
}

const round2 = (n: number) => Math.round(n * 100) / 100

/** The exits from PRD.md §5.1's example strategy. Phase 2 reads these off
 * the strategy document itself; Phase 1 has one shape for all of them. */
const DEFAULT_MANAGED_EXIT: ManagedExit = { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 }

/** Keeps the value chart honest after the position changes size. Without
 * this the series still ends at the old value and the chart contradicts
 * the row it expands from — the one thing the fixture invariant exists to
 * prevent, undone at runtime. */
function withValuePoint(position: Position, at: string, value: number): PricePoint[] {
  const date = at.slice(0, 10)
  const history = position.valueHistory
  const last = history[history.length - 1]
  return last.date === date ? [...history.slice(0, -1), { date, value }] : [...history, { date, value }]
}

/** Returns the position that remains after selling `quantity` of it, or
 * null when the whole thing is gone.
 *
 * A partial close scales cost basis, value and P&L by what's left, so the
 * row keeps satisfying `value === last × quantity × 100`. It does not
 * scale `pnlPct`: closing half a position doesn't change how well the
 * trade did, only how much of it you still hold. */
function closeQuantity(position: Position, quantity: number, at: string): Position | null {
  if (quantity >= position.quantity) return null

  const remaining = position.quantity - quantity
  const fraction = remaining / position.quantity
  const value = round2(position.value * fraction)

  return {
    ...position,
    quantity: remaining,
    costBasis: round2(position.costBasis * fraction),
    value,
    pnl: round2(position.pnl * fraction),
    pnlPct: position.pnlPct,
    // The old exit was written for the old size and no longer describes
    // this position. One position, one closing order, always current.
    attachedExit: null,
    valueHistory: withValuePoint(position, at, value),
  }
}

function addQuantity(position: Position, quantity: number, price: number, at: string): Position {
  const newQuantity = position.quantity + quantity
  const costBasis = round2(position.costBasis + price * quantity * CONTRACT_MULTIPLIER)
  const value = round2(position.last * newQuantity * CONTRACT_MULTIPLIER)
  // A long gains as value rises above what it paid; a short as value falls
  // below what it took in.
  const pnl = round2(position.direction === 'long' ? value - costBasis : costBasis - value)

  return {
    ...position,
    quantity: newQuantity,
    costBasis,
    value,
    pnl,
    pnlPct: costBasis === 0 ? 0 : round2((pnl / costBasis) * 100),
    valueHistory: withValuePoint(position, at, value),
  }
}

/** The tick's price stream. Seeded, so a session replays identically and a
 * screenshot taken twice looks the same — the rule the fixtures already
 * follow, extended to the thing that moves them. */
const priceStream = mulberry32(20261101)

/** Re-marks a position at a new contract price, carrying bid, ask, value
 * and P&L with it so the row stays internally consistent — the same
 * invariants `orders.test.ts` asserts about the fixtures. */
function remark(position: Position, price: number, at: string): Position {
  const half = round2((position.ask - position.bid) / 2)
  const last = round2(Math.max(price, 0.01))
  const value = round2(last * position.quantity * CONTRACT_MULTIPLIER)
  const pnl = round2(position.direction === 'long' ? value - position.costBasis : position.costBasis - value)

  return {
    ...position,
    last,
    bid: round2(Math.max(last - half, 0.01)),
    ask: round2(last + half),
    value,
    pnl,
    pnlPct: position.costBasis === 0 ? 0 : round2((pnl / position.costBasis) * 100),
    valueHistory: withValuePoint(position, at, value),
  }
}

/** Applies a change to one position in the active account's book, leaving
 * the other account untouched. Returns state unchanged if the id belongs
 * to a book whose credentials aren't loaded. */
function updatePosition(s: UIState, id: string, fn: (p: Position) => Position): Partial<UIState> {
  const mode = s.accountMode
  if (!s.openPositions[mode].some((p) => p.id === id)) return s
  return {
    openPositions: {
      ...s.openPositions,
      [mode]: s.openPositions[mode].map((p) => (p.id === id ? fn(p) : p)),
    },
  }
}

export const useUIStore = create<UIState>((set) => ({
  theme: 'light',
  accountMode: 'paper',
  executionMode: 'manual',
  paletteOpen: false,
  activeStrategyId: 'strat-1',
  isHalted: false,
  openPositions: {
    paper: ACCOUNT_SNAPSHOTS.paper.positions,
    cash: ACCOUNT_SNAPSHOTS.cash.positions,
  },
  activity: {
    paper: ACCOUNT_SNAPSHOTS.paper.activity,
    cash: ACCOUNT_SNAPSHOTS.cash.activity,
  },
  workingOrders: {
    paper: ACCOUNT_SNAPSHOTS.paper.workingOrders,
    cash: ACCOUNT_SNAPSHOTS.cash.workingOrders,
  },
  toggleTheme: () =>
    set((s) => ({ theme: s.theme === 'light' ? 'dark' : 'light' })),
  setAccountMode: (accountMode) => set({ accountMode }),
  setExecutionMode: (executionMode) => set({ executionMode }),
  setActiveStrategyId: (activeStrategyId) => set({ activeStrategyId }),
  openPalette: () => set({ paletteOpen: true }),
  closePalette: () => set({ paletteOpen: false }),
  halt: () => set({ isHalted: true }),
  resume: () => set({ isHalted: false }),
  flatten: () =>
    set((s) => {
      // Only the active account's book is touched. That is not a partial
      // flatten: `accountMode` is which Alpaca keys are in use, so the
      // engine only ever holds the positions in that account. The other
      // book belongs to credentials that aren't loaded.
      const mode = s.accountMode
      // Every close is an execution and shows up in the activity feed, the
      // same as it would coming back from the broker.
      const at = new Date().toISOString()
      return {
        openPositions: { ...s.openPositions, [mode]: [] },
        activity: {
          ...s.activity,
          [mode]: [...s.openPositions[mode].map((p) => closeExecution(p, at, 'act-flat-')), ...s.activity[mode]],
        },
        // Flatten closes everything, so every working order in this book is
        // now an order against a position that no longer exists. Leaving
        // them would show orders that can never fill.
        workingOrders: { ...s.workingOrders, [mode]: [] },
        isHalted: true,
      }
    }),
  submitPositionOrder: (id, draft) =>
    set((s) => {
      const mode = s.accountMode
      const position = s.openPositions[mode].find((p) => p.id === id)
      if (!position) return s

      const at = new Date().toISOString()
      const { pricePerContract, side } = estimate(position, draft)
      const quantity = Math.min(draft.quantity, draft.mode === 'close' ? position.quantity : draft.quantity)
      const contract = `${position.symbol} ${position.contract}`

      // A market order fills. Anything else sits and works until it does,
      // which is the whole reason working orders exist — a terminal that
      // fills every order instantly cannot show you an order you regret.
      if (isWorkingOrderType(draft.orderType)) {
        const activityId = `act-${draft.mode}-${position.id}-${at}`
        const pending: ActivityItem = {
          id: activityId,
          time: at,
          contract,
          action: side,
          price: draft.limitPrice ?? draft.stopPrice,
          quantity,
          // Nothing has happened yet, so there is nothing to report.
          pnl: null,
          pnlPct: null,
          amount: null,
          status: 'pending',
        }
        const order: WorkingOrder = {
          id: `wo-${position.id}-${at}`,
          positionId: position.id,
          contract,
          side,
          orderType: draft.orderType,
          quantity,
          limitPrice: draft.limitPrice,
          stopPrice: draft.stopPrice,
          timeInForce: draft.timeInForce,
          placedAt: at,
          activityId,
        }
        return {
          workingOrders: { ...s.workingOrders, [mode]: [order, ...s.workingOrders[mode]] },
          activity: { ...s.activity, [mode]: [pending, ...s.activity[mode]] },
        }
      }

      const fill: ActivityItem = {
        id: `act-${draft.mode}-${position.id}-${at}`,
        time: at,
        contract,
        action: side,
        price: pricePerContract,
        quantity,
        // An opening fill has realized nothing; only a close reports P&L.
        pnl: draft.mode === 'close' ? round2(position.pnl * (quantity / position.quantity)) : null,
        pnlPct: draft.mode === 'close' ? position.pnlPct : null,
        amount: null,
        status: 'filled',
      }

      const next =
        draft.mode === 'close'
          ? closeQuantity(position, quantity, at)
          : addQuantity(position, quantity, pricePerContract, at)
      const closedOut = next === null

      // Note what this does *not* do: it never sets isHalted. Acting on one
      // position is not a book-wide event (CLAUDE.md rule 7).
      return {
        openPositions: {
          ...s.openPositions,
          [mode]: closedOut
            ? s.openPositions[mode].filter((p) => p.id !== id)
            : s.openPositions[mode].map((p) => (p.id === id ? next : p)),
        },
        activity: { ...s.activity, [mode]: [fill, ...s.activity[mode]] },
        // A working order against a position that no longer exists is an
        // order that can never fill. Closing out takes its orders with it.
        workingOrders: closedOut
          ? { ...s.workingOrders, [mode]: s.workingOrders[mode].filter((o) => o.positionId !== id) }
          : s.workingOrders,
      }
    }),
  lastTickAt: null,
  tick: () =>
    set((s) => {
      const mode = s.accountMode
      const at = new Date().toISOString()
      const filled: ActivityItem[] = []
      const closedIds = new Set<string>()
      const consumedOrderIds = new Set<string>()

      const positions = s.openPositions[mode].map((position) => {
        // ±1.8% a tick, which is brisk for a stock and ordinary for an
        // option. Enough movement that a resting order is reachable
        // without waiting all afternoon to see the feature work.
        const drift = (priceStream() - 0.5) * 0.036
        const marked = remark(position, position.last * (1 + drift), at)

        const exit = marked.attachedExit
        const trigger = exit ? exitTrigger(marked, exit, marked.last) : null
        if (exit && trigger) {
          const price = trigger === 'take_profit' ? exit.takeProfit : (exit.stopLimitPrice ?? exit.stopPrice)
          filled.push({
            id: `act-exit-${marked.id}-${at}`,
            time: at,
            contract: `${marked.symbol} ${marked.contract}`,
            action: marked.direction === 'long' ? 'STC' : 'BTC',
            price,
            quantity: marked.quantity,
            pnl: marked.pnl,
            pnlPct: marked.pnlPct,
            amount: null,
            status: 'filled',
          })
          closedIds.add(marked.id)
          return marked
        }

        return marked
      })

      // Working orders fill against the price their own position just
      // reached. An order on a position that closed out this same tick can
      // no longer fill, so it is dropped rather than matched.
      const byId = new Map(positions.map((p) => [p.id, p]))
      const remainingOrders = s.workingOrders[mode].filter((order) => {
        const position = byId.get(order.positionId)
        if (!position || closedIds.has(position.id)) return false
        if (!orderWouldFill(order, position.last)) return true

        consumedOrderIds.add(order.id)
        if (order.side === 'STC' || order.side === 'BTC') closedIds.add(position.id)
        return false
      })

      if (filled.length === 0 && consumedOrderIds.size === 0) {
        return {
          openPositions: { ...s.openPositions, [mode]: positions },
          lastTickAt: at,
        }
      }

      // A working order that filled turns its pending ledger row into a
      // fill, rather than writing a second row beside it.
      const activity = s.activity[mode].map((a) => {
        const order = s.workingOrders[mode].find((o) => o.activityId === a.id && consumedOrderIds.has(o.id))
        if (!order) return a
        const position = byId.get(order.positionId)
        const closing = order.side === 'STC' || order.side === 'BTC'
        return {
          ...a,
          status: 'filled' as const,
          price: order.limitPrice ?? order.stopPrice,
          pnl: closing && position ? position.pnl : null,
          pnlPct: closing && position ? position.pnlPct : null,
        }
      })

      return {
        openPositions: {
          ...s.openPositions,
          [mode]: positions.filter((p) => !closedIds.has(p.id)),
        },
        workingOrders: { ...s.workingOrders, [mode]: remainingOrders },
        activity: { ...s.activity, [mode]: [...filled, ...activity] },
        lastTickAt: at,
      }
    }),
  cancelWorkingOrder: (id) =>
    set((s) => {
      const mode = s.accountMode
      const order = s.workingOrders[mode].find((o) => o.id === id)
      if (!order) return s

      return {
        workingOrders: { ...s.workingOrders, [mode]: s.workingOrders[mode].filter((o) => o.id !== id) },
        activity: {
          ...s.activity,
          [mode]: s.activity[mode].map((a) =>
            a.id === order.activityId ? { ...a, status: 'canceled' } : a,
          ),
        },
      }
    }),
  upsertExit: (id, exit) =>
    set((s) => updatePosition(s, id, (p) => ({
      ...p,
      attachedExit: exit,
      // Placing a manual exit takes the position off its strategy's rules.
      strategyId: null,
      managedExit: null,
    }))),
  cancelExit: (id) => set((s) => updatePosition(s, id, (p) => ({ ...p, attachedExit: null }))),
  detachFromStrategy: (id) =>
    set((s) => updatePosition(s, id, (p) => ({ ...p, strategyId: null, managedExit: null }))),
  reattachToStrategy: (id) =>
    set((s) => updatePosition(s, id, (p) => ({
      ...p,
      // Back to the strategy that opened it, not to whichever one is
      // active now — those are different sets of exit rules.
      strategyId: p.openedByStrategyId,
      managedExit: DEFAULT_MANAGED_EXIT,
      // The strategy's rules and a manual exit cannot both run.
      attachedExit: null,
    }))),
}))
