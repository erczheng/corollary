import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from './store'
import { ACCOUNT_SNAPSHOTS, CHAT_HISTORY, NOTIFICATIONS, RISK_LIMITS } from './mockData'
import type { OrderDraft } from './orders'
import { unreadCount, visibleNotifications } from './notifications'
import { notificationAuditField } from './settings'

const initialState = useUIStore.getState()

const PAPER = ACCOUNT_SNAPSHOTS.paper
const CASH = ACCOUNT_SNAPSHOTS.cash

beforeEach(() => {
  useUIStore.setState(initialState, true)
})

/** CLAUDE.md rule 7 and PRD.md §2: Halt and Flatten are distinct actions
 * and are never merged. These tests exist because the two once had
 * identical bodies — both set `isHalted` and neither touched a position —
 * which typechecked, rendered, and was wrong. */
describe('halt vs flatten', () => {
  it('halt stops new entries and leaves every open position alone', () => {
    useUIStore.getState().halt()

    const s = useUIStore.getState()
    expect(s.isHalted).toBe(true)
    expect(s.openPositions.paper).toHaveLength(PAPER.positions.length)
    expect(s.activity.paper).toHaveLength(PAPER.activity.length)
  })

  it('flatten closes every position, then halts', () => {
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.openPositions.paper).toHaveLength(0)
    expect(s.isHalted).toBe(true)
  })

  it('halt and flatten do not have the same effect', () => {
    useUIStore.getState().halt()
    const afterHalt = useUIStore.getState().openPositions.paper.length

    useUIStore.setState(initialState, true)
    useUIStore.getState().flatten()
    const afterFlatten = useUIStore.getState().openPositions.paper.length

    expect(afterHalt).not.toBe(afterFlatten)
  })

  it('resume lifts a halt without reopening anything', () => {
    useUIStore.getState().flatten()
    useUIStore.getState().resume()

    const s = useUIStore.getState()
    expect(s.isHalted).toBe(false)
    expect(s.openPositions.paper).toHaveLength(0)
  })
})

describe('flatten executions', () => {
  it('records one execution per closed position, newest first', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    expect(activity.paper).toHaveLength(PAPER.activity.length + PAPER.positions.length)
    expect(activity.paper.slice(0, PAPER.positions.length).every((a) => a.status === 'filled')).toBe(true)
  })

  it('sells a long to close at the bid and buys a short to close at the ask', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    const long = PAPER.positions.find((p) => p.direction === 'long')!
    const short = PAPER.positions.find((p) => p.direction === 'short')!

    const longClose = activity.paper.find((a) => a.id === `act-flat-${long.id}`)!
    const shortClose = activity.paper.find((a) => a.id === `act-flat-${short.id}`)!

    expect(longClose.action).toBe('STC')
    expect(longClose.price).toBe(long.bid)
    expect(shortClose.action).toBe('BTC')
    expect(shortClose.price).toBe(short.ask)
  })

  it('carries each position P&L, in dollars and percent, onto its closing execution', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    for (const p of PAPER.positions) {
      const close = activity.paper.find((a) => a.id === `act-flat-${p.id}`)!
      expect(close.pnl).toBe(p.pnl)
      expect(close.pnlPct).toBe(p.pnlPct)
    }
  })

  it('flattening an already-empty book halts without inventing executions', () => {
    useUIStore.getState().flatten()
    const afterFirst = useUIStore.getState().activity.paper.length

    useUIStore.getState().resume()
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.activity.paper).toHaveLength(afterFirst)
    expect(s.isHalted).toBe(true)
  })
})

/** Paper and Cash are different accounts holding different money, and only
 * one account's keys are in use at a time. An action taken while looking at
 * one book must not reach into the other. */
describe('the two accounts are separate books', () => {
  it('flatten empties only the account whose keys are in use', () => {
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.openPositions.paper).toHaveLength(0)
    expect(s.openPositions.cash).toHaveLength(CASH.positions.length)
    expect(s.activity.cash).toHaveLength(CASH.activity.length)
  })

  it('flattens the cash book, and only the cash book, once Cash is selected', () => {
    useUIStore.getState().setAccountMode('cash')
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.openPositions.cash).toHaveLength(0)
    expect(s.openPositions.paper).toHaveLength(PAPER.positions.length)
  })

  it('starts each account with its own positions and its own feed', () => {
    const s = useUIStore.getState()
    expect(s.openPositions.paper).not.toEqual(s.openPositions.cash)
    expect(s.activity.paper.length).not.toBe(s.activity.cash.length)
  })
})

/** Close is to Flatten what Flatten is to Halt: a narrower action that must
 * not quietly do the wider one. Closing a single position from Activity is
 * not a book-wide event and never halts the engine. */
const closeDraft = (quantity: number): OrderDraft => ({
  mode: 'close',
  quantity,
  orderType: 'market',
  limitPrice: null,
  stopPrice: null,
  timeInForce: 'day',
})

describe('submitPositionOrder — closing', () => {
  it('removes just that position and leaves the rest of the book open', () => {
    const target = PAPER.positions[1]
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(target.quantity))

    const s = useUIStore.getState()
    expect(s.openPositions.paper).toHaveLength(PAPER.positions.length - 1)
    expect(s.openPositions.paper.some((p) => p.id === target.id)).toBe(false)
  })

  it('does not halt the engine', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(target.quantity))

    expect(useUIStore.getState().isHalted).toBe(false)
  })

  it('logs the close as an execution, priced on the side it crossed', () => {
    const short = PAPER.positions.find((p) => p.direction === 'short')!
    useUIStore.getState().submitPositionOrder(short.id, closeDraft(short.quantity))

    const close = useUIStore.getState().activity.paper[0]
    expect(close.action).toBe('BTC')
    expect(close.price).toBe(short.ask)
    expect(close.status).toBe('filled')
  })

  it('ignores an id that is not in the active account', () => {
    const before = useUIStore.getState()
    useUIStore.getState().submitPositionOrder(CASH.positions[0].id, closeDraft(1))

    const after = useUIStore.getState()
    expect(after.openPositions.paper).toEqual(before.openPositions.paper)
    expect(after.activity.paper).toEqual(before.activity.paper)
  })

  /** A partial close leaves a smaller position that still has to satisfy
   * everything the row asserts about itself. */
  it('scales what remains, keeping value = mark × quantity × 100', () => {
    const target = PAPER.positions.find((p) => p.quantity > 1)!
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(1))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.quantity).toBe(target.quantity - 1)
    expect(after.value).toBeCloseTo(after.last * after.quantity * 100, 2)
    expect(after.costBasis).toBeLessThan(target.costBasis)
    // Closing half a trade doesn't change how well the trade did.
    expect(after.pnlPct).toBe(target.pnlPct)
  })

  it('realizes only the P&L on the contracts actually closed', () => {
    const target = PAPER.positions.find((p) => p.quantity > 1 && p.pnl !== 0)!
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(1))

    const fill = useUIStore.getState().activity.paper[0]
    expect(fill.pnl).toBeCloseTo(target.pnl / target.quantity, 2)
  })

  it('ends the value history at the position’s new value', () => {
    const target = PAPER.positions.find((p) => p.quantity > 1)!
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(1))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.valueHistory[after.valueHistory.length - 1].value).toBe(after.value)
  })
})

/** `closeQuantity` leaves `pnlPct` untouched while `addQuantity`
 * recomputes it, which looks like an inconsistency and is not: a partial
 * close divides `pnl` and `costBasis` by the same fraction, so the ratio
 * is unchanged. This pins that, because the next person to read those two
 * functions will have the same doubt and should get an answer from the
 * suite rather than from arithmetic on a napkin. */
describe('pnlPct stays consistent with pnl over costBasis', () => {
  const sequences: ('close' | 'add')[][] = [['close'], ['add'], ['close', 'add'], ['add', 'close']]

  for (const seq of sequences) {
    it(`holds after ${seq.join(' then ')}`, () => {
      const target = PAPER.positions.find((p) => p.quantity > 1)!
      for (const mode of seq) {
        useUIStore.getState().submitPositionOrder(target.id, { ...closeDraft(1), mode })
      }

      const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
      const derived = (after.pnl / after.costBasis) * 100
      // Within rounding: every field is stored to the cent.
      expect(Math.abs(after.pnlPct - derived)).toBeLessThan(0.01)
    })
  }
})

describe('submitPositionOrder — adding', () => {
  it('adds to a long by buying to open and to a short by selling to open', () => {
    const long = PAPER.positions.find((p) => p.direction === 'long')!
    const short = PAPER.positions.find((p) => p.direction === 'short')!

    useUIStore.getState().submitPositionOrder(long.id, { ...closeDraft(1), mode: 'add' })
    expect(useUIStore.getState().activity.paper[0].action).toBe('BTO')

    useUIStore.getState().submitPositionOrder(short.id, { ...closeDraft(1), mode: 'add' })
    expect(useUIStore.getState().activity.paper[0].action).toBe('STO')
  })

  it('grows the position and re-derives its value and P&L', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().submitPositionOrder(target.id, { ...closeDraft(2), mode: 'add' })

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.quantity).toBe(target.quantity + 2)
    expect(after.value).toBeCloseTo(after.last * after.quantity * 100, 2)
    const expected = after.direction === 'long' ? after.value - after.costBasis : after.costBasis - after.value
    expect(after.pnl).toBeCloseTo(expected, 2)
  })

  it('reports no P&L on an opening fill, because none is realized', () => {
    useUIStore.getState().submitPositionOrder(PAPER.positions[0].id, { ...closeDraft(1), mode: 'add' })

    const fill = useUIStore.getState().activity.paper[0]
    expect(fill.pnl).toBeNull()
    expect(fill.pnlPct).toBeNull()
  })
})

/** The tick stands in for the Alpaca WebSocket. It is the mock *broker* —
 * it decides what the market did, never what is allowed, which stays with
 * the risk manager. */
describe('the price tick', () => {
  it('re-marks positions and keeps every row invariant intact', () => {
    useUIStore.getState().tick()

    for (const p of useUIStore.getState().openPositions.paper) {
      expect(p.last).toBeGreaterThanOrEqual(p.bid)
      expect(p.last).toBeLessThanOrEqual(p.ask)
      expect(p.value).toBeCloseTo(p.last * p.quantity * 100, 2)
      const expected = p.direction === 'long' ? p.value - p.costBasis : p.costBasis - p.value
      expect(p.pnl).toBeCloseTo(expected, 2)
      expect(p.valueHistory[p.valueHistory.length - 1].value).toBe(p.value)
    }
  })

  it('moves the underlying too, not just the contract', () => {
    const before = useUIStore.getState().underlyings
    useUIStore.getState().tick()
    const after = useUIStore.getState().underlyings

    // A page that streams contract prices while the stock behind them sits
    // frozen is only half live, and the payoff chart's "now" marker would
    // never move.
    const held = new Set(PAPER.positions.map((p) => p.symbol))
    for (const symbol of held) {
      expect(after[symbol].price).not.toBe(before[symbol].price)
      // Today's point *is* today's price, so it moves rather than a new
      // daily close being appended every two seconds.
      expect(after[symbol].history).toHaveLength(before[symbol].history.length)
      expect(after[symbol].history[after[symbol].history.length - 1].value).toBe(after[symbol].price)
      // The day is still measured from yesterday's close.
      expect(after[symbol].change).toBeCloseTo(after[symbol].price - after[symbol].previousClose, 2)
    }
  })

  it('keeps every position in step with the quote for its symbol', () => {
    useUIStore.getState().tick()

    for (const p of useUIStore.getState().openPositions.paper) {
      expect(p.underlying).toBe(useUIStore.getState().underlyings[p.symbol].price)
    }
  })

  it('streams only the symbols the active account actually holds', () => {
    const before = useUIStore.getState().underlyings
    useUIStore.getState().tick()
    const after = useUIStore.getState().underlyings

    // The 30-symbol cap on the Basic plan is why the subscription is
    // scoped to open positions rather than to every symbol we know about.
    const held = new Set(PAPER.positions.map((p) => p.symbol))
    for (const symbol of Object.keys(before)) {
      if (!held.has(symbol)) expect(after[symbol]).toBe(before[symbol])
    }
  })

  /** Volatility is stated per second and scaled by the tick, so update
   * frequency and price movement are independent knobs. Stated per tick
   * they are welded together, and raising the rate to make the page feel
   * more responsive would also make it that much more volatile. */
  it('scales movement to the time a tick covers, not to how often it fires', () => {
    const before = useUIStore.getState().openPositions.paper.map((p) => p.last)
    useUIStore.getState().tick(50)
    const after = useUIStore.getState().openPositions.paper.map((p) => p.last)

    after.forEach((price, i) => {
      // 50ms at 1.8%/s is at most 0.09%, plus a cent of rounding.
      const bound = before[i] * 0.0009 + 0.005
      expect(Math.abs(price - before[i])).toBeLessThanOrEqual(bound + 1e-9)
    })
  })

  it('moves further over a longer tick', () => {
    const before = useUIStore.getState().openPositions.paper.map((p) => p.last)
    useUIStore.getState().tick(4_000)
    const after = useUIStore.getState().openPositions.paper.map((p) => p.last)

    // 4s at 1.8%/s permits up to 7.2% — far outside what 50ms allows, so
    // this fails if the elapsed argument is ever ignored.
    after.forEach((price, i) => {
      expect(Math.abs(price - before[i])).toBeLessThanOrEqual(before[i] * 0.072 + 0.005 + 1e-9)
    })
    expect(after.some((price, i) => Math.abs(price - before[i]) > before[i] * 0.0009)).toBe(true)
  })

  it('records when the last price arrived', () => {
    expect(useUIStore.getState().lastTickAt).toBeNull()
    useUIStore.getState().tick()
    expect(useUIStore.getState().lastTickAt).not.toBeNull()
  })

  it('ticks only the account whose keys are in use', () => {
    const before = useUIStore.getState().openPositions.cash
    useUIStore.getState().tick()

    // The other book has no stream behind it.
    expect(useUIStore.getState().openPositions.cash).toBe(before)
  })

  it('fills a working order once the price reaches it, and closes the position', () => {
    const target = PAPER.positions.find((p) => p.id === 'pos-2')!
    // A sell limit a long way below the mark: the next tick must reach it.
    useUIStore.setState({
      workingOrders: {
        paper: [
          {
            id: 'wo-fill',
            positionId: target.id,
            contractKey: null,
            contract: `${target.symbol} ${target.contract}`,
            side: 'STC',
            orderType: 'limit',
            quantity: target.quantity,
            limitPrice: 0.01,
            stopPrice: null,
            timeInForce: 'gtc',
            placedAt: '2026-08-07T15:00:00Z',
            activityId: 'act-1',
          },
        ],
        cash: [],
      },
    })

    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.workingOrders.paper).toHaveLength(0)
    expect(s.openPositions.paper.some((p) => p.id === target.id)).toBe(false)
    // The order's own pending row becomes the fill, rather than a second
    // row appearing beside it.
    const row = s.activity.paper.find((a) => a.id === 'act-1')!
    expect(row.status).toBe('filled')
    expect(row.pnl).not.toBeNull()
  })

  it('triggers an attached exit and logs the close', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().upsertExit(target.id, {
      // Take-profit at a cent: any tick reaches it.
      takeProfit: 0.01,
      stopPrice: 0.001,
      stopLimitPrice: null,
      timeInForce: 'gtc',
      heldBy: 'broker',
    })
    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.openPositions.paper.some((p) => p.id === target.id)).toBe(false)
    expect(s.activity.paper[0].status).toBe('filled')
    expect(s.activity.paper[0].pnl).not.toBeNull()
  })

  it('leaves a resting order alone when the price has not reached it', () => {
    const target = PAPER.positions.find((p) => p.id === 'pos-2')!
    useUIStore.setState({
      workingOrders: {
        paper: [
          {
            id: 'wo-far',
            positionId: target.id,
            contractKey: null,
            contract: `${target.symbol} ${target.contract}`,
            side: 'STC',
            orderType: 'limit',
            quantity: 1,
            // Far above any plausible tick.
            limitPrice: 10_000,
            stopPrice: null,
            timeInForce: 'gtc',
            placedAt: '2026-08-07T15:00:00Z',
            activityId: 'act-1',
          },
        ],
        cash: [],
      },
    })

    useUIStore.getState().tick()

    expect(useUIStore.getState().workingOrders.paper).toHaveLength(1)
    expect(useUIStore.getState().activity.paper.find((a) => a.id === 'act-1')!.status).toBe('pending')
  })

  it('replays identically, because the price stream is seeded', () => {
    useUIStore.getState().tick()
    const first = useUIStore.getState().openPositions.paper.map((p) => p.last)

    // Same store, same fixtures, same stream position — a screenshot taken
    // twice has to look the same, which is why nothing here uses
    // Math.random().
    expect(first.every((v) => Number.isFinite(v))).toBe(true)
    expect(first).toHaveLength(PAPER.positions.length)
  })
})

/** One position, one closing order. Two exits on one position double-close
 * when a cancel races a fill, and the failure is silent. */
describe('attached exits', () => {
  const exit = {
    takeProfit: 3.0,
    stopPrice: 1.5,
    stopLimitPrice: null,
    timeInForce: 'gtc' as const,
    heldBy: 'broker' as const,
  }

  it('attaches an exit and detaches the position from its strategy', () => {
    const target = PAPER.positions.find((p) => p.strategyId !== null)!
    useUIStore.getState().upsertExit(target.id, exit)

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.attachedExit).toEqual(exit)
    // Manual replaces managed — the two never run at once.
    expect(after.strategyId).toBeNull()
    expect(after.managedExit).toBeNull()
  })

  it('edits in place rather than stacking a second exit', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().upsertExit(target.id, exit)
    useUIStore.getState().upsertExit(target.id, { ...exit, takeProfit: 4.0 })

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.attachedExit).toEqual({ ...exit, takeProfit: 4.0 })
  })

  it('drops the attached exit when the position is partially closed', () => {
    const target = PAPER.positions.find((p) => p.quantity > 1)!
    useUIStore.getState().upsertExit(target.id, exit)
    useUIStore.getState().submitPositionOrder(target.id, closeDraft(1))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    // The old exit was written for the old size and no longer describes it.
    expect(after.attachedExit).toBeNull()
  })

  it('cancels an exit without reattaching the strategy', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().upsertExit(target.id, exit)
    useUIStore.getState().cancelExit(target.id)

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.attachedExit).toBeNull()
    expect(after.strategyId).toBeNull()
  })

  it('reattaching to a strategy clears the manual exit', () => {
    const target = PAPER.positions[0]
    useUIStore.getState().upsertExit(target.id, exit)
    useUIStore.getState().reattachToStrategy(target.id)

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.strategyId).toBe(target.openedByStrategyId)
    expect(after.managedExit).not.toBeNull()
    expect(after.attachedExit).toBeNull()
  })

  it('detaching leaves the position open with no exit regime at all', () => {
    const target = PAPER.positions.find((p) => p.strategyId !== null)!
    useUIStore.getState().detachFromStrategy(target.id)

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.strategyId).toBeNull()
    expect(after.managedExit).toBeNull()
    expect(after.attachedExit).toBeNull()
  })
})

/** The Markets poll. Distinct from `tick` on purpose — it stands in for a
 * snapshot request across the quoted universe, where the tick stands in
 * for a 30-symbol websocket scoped to open positions. */
describe('pollMarkets', () => {
  it('moves every quoted symbol, not just the ones behind a position', () => {
    const before = { ...useUIStore.getState().underlyings }
    useUIStore.getState().pollMarkets(2_000)
    const after = useUIStore.getState().underlyings

    // A screener that only moves the six stocks you happen to hold is not
    // a screener.
    const moved = Object.keys(after).filter((s) => after[s].price !== before[s].price)
    expect(moved.length).toBeGreaterThan(6)
    expect(Object.keys(after).length).toBe(Object.keys(before).length)
  })

  it('keeps the day change anchored to yesterday, not to the last poll', () => {
    useUIStore.getState().pollMarkets(2_000)
    for (const q of Object.values(useUIStore.getState().underlyings)) {
      expect(q.change).toBeCloseTo(q.price - q.previousClose, 2)
    }
  })

  it('re-prices the chain from its underlying, keeping the ladder in order', () => {
    // The invariant that breaks if contracts are walked independently: a
    // 225 call printing above the 220 beside it is an arbitrage, and the
    // chain stops reading like a chain within seconds.
    for (let i = 0; i < 20; i++) useUIStore.getState().pollMarkets(2_000)
    const chain = useUIStore.getState().chain

    for (const symbol of new Set(chain.map((c) => c.symbol))) {
      for (const expiration of new Set(chain.map((c) => c.expiration))) {
        for (const type of ['call', 'put'] as const) {
          const ladder = chain
            .filter((c) => c.symbol === symbol && c.expiration === expiration && c.type === type)
            .sort((a, b) => a.strike - b.strike)

          for (let i = 1; i < ladder.length; i++) {
            if (type === 'call') expect(ladder[i].last).toBeLessThan(ladder[i - 1].last)
            else expect(ladder[i].last).toBeGreaterThan(ladder[i - 1].last)
          }
        }
      }
    }
  })

  it('keeps every quote inside its own spread, poll after poll', () => {
    for (let i = 0; i < 20; i++) useUIStore.getState().pollMarkets(2_000)
    for (const c of useUIStore.getState().chain) {
      expect(c.bid).toBeGreaterThan(0)
      expect(c.ask).toBeGreaterThan(c.bid)
      expect(c.last).toBeGreaterThanOrEqual(c.bid)
      expect(c.last).toBeLessThanOrEqual(c.ask)
    }
  })

  it('only ever accumulates volume', () => {
    // A screener sorted on a figure that can fall would reorder backwards
    // mid-session.
    const before = new Map(
      useUIStore.getState().chain.map((c) => [`${c.symbol}${c.strike}${c.type}${c.expiration}`, c.volume]),
    )
    for (let i = 0; i < 5; i++) useUIStore.getState().pollMarkets(2_000)
    for (const c of useUIStore.getState().chain) {
      expect(c.volume).toBeGreaterThanOrEqual(
        before.get(`${c.symbol}${c.strike}${c.type}${c.expiration}`)!,
      )
    }
  })

  it('leaves the opening fixture untouched, so a reload replays the same session', () => {
    const opening = useUIStore.getState().chain[0]
    const snapshot = { ...opening }
    for (let i = 0; i < 5; i++) useUIStore.getState().pollMarkets(2_000)
    expect(opening).toEqual(snapshot)
  })
})

describe('submitOpenOrder', () => {
  const contract = {
    symbol: 'AAPL',
    strike: 230,
    expiration: '2026-08-21',
    type: 'call' as const,
    last: 7.41,
    previousClose: 7.97,
    change: -0.56,
    changePct: -7.03,
    bid: 7.31,
    ask: 7.51,
    volume: 26_056,
    openInterest: 87_887,
    iv: 0.284,
  }

  const draft = {
    side: 'BTO' as const,
    quantity: 2,
    orderType: 'market' as const,
    limitPrice: null,
    stopPrice: null,
    timeInForce: 'day' as const,
  }

  it('opens a long that is flat at the fill, not already in profit', () => {
    useUIStore.getState().submitOpenOrder(contract, draft)
    const position = useUIStore.getState().openPositions.paper[0]

    // Bought at the ask and marked at the ask: a position that opens
    // showing a gain has been marked against the wrong side of the spread.
    expect(position.direction).toBe('long')
    expect(position.quantity).toBe(2)
    expect(position.costBasis).toBe(1_502)
    expect(position.symbol).toBe('AAPL')
    expect(position.expiry).toBe('2026-08-21')
    expect(position.legs[0].symbol).toBe('AAPL260821C00230000')
  })

  it('opens a short whose cost basis is the credit taken in', () => {
    useUIStore.getState().submitOpenOrder(contract, { ...draft, side: 'STO' })
    const position = useUIStore.getState().openPositions.paper[0]

    expect(position.direction).toBe('short')
    // Sold at the bid.
    expect(position.costBasis).toBe(1_462)
    expect(position.legs[0].side).toBe('short')
  })

  it('writes a filled opening row that reports no P&L', () => {
    useUIStore.getState().submitOpenOrder(contract, draft)
    const [row] = useUIStore.getState().activity.paper

    expect(row.status).toBe('filled')
    expect(row.action).toBe('BTO')
    // An opening fill has realized nothing; only a close reports P&L.
    expect(row.pnl).toBeNull()
    expect(row.pnlPct).toBeNull()
  })

  it('opens the position under no strategy, so nothing else manages it', () => {
    useUIStore.getState().submitOpenOrder(contract, draft)
    const position = useUIStore.getState().openPositions.paper[0]

    // Bought by hand from the chain. Two exit regimes on one position
    // double-close when a cancel races a fill.
    expect(position.strategyId).toBeNull()
    expect(position.managedExit).toBeNull()
    expect(position.attachedExit).toBeNull()
  })

  it('rests a limit order instead of filling it, and fills it from the poll', () => {
    // A limit far above the mark fills on the first poll; the point is that
    // it goes through the working-orders list rather than straight to a
    // position.
    useUIStore.getState().submitOpenOrder(contract, {
      ...draft,
      orderType: 'limit',
      limitPrice: 99,
    })

    expect(useUIStore.getState().openPositions.paper).toHaveLength(
      ACCOUNT_SNAPSHOTS.paper.positions.length,
    )
    const order = useUIStore.getState().workingOrders.paper[0]
    expect(order.positionId).toBeNull()
    expect(order.contractKey).toBe('AAPL-2026-08-21-230-call')
    expect(useUIStore.getState().activity.paper[0].status).toBe('pending')

    useUIStore.getState().pollMarkets(2_000)

    expect(useUIStore.getState().workingOrders.paper.some((o) => o.id === order.id)).toBe(false)
    expect(useUIStore.getState().openPositions.paper.length).toBe(
      ACCOUNT_SNAPSHOTS.paper.positions.length + 1,
    )
    expect(useUIStore.getState().activity.paper.find((a) => a.id === order.activityId)?.status).toBe(
      'filled',
    )
  })

  it('opens into the account whose keys are loaded, and only that one', () => {
    useUIStore.setState({ accountMode: 'cash' })
    useUIStore.getState().submitOpenOrder(contract, draft)

    expect(useUIStore.getState().openPositions.cash.length).toBe(CASH.positions.length + 1)
    expect(useUIStore.getState().openPositions.paper.length).toBe(PAPER.positions.length)
  })
})

// ---------------------------------------------------------------------- //
// Settings (PRD.md §8.7)
// ---------------------------------------------------------------------- //

/** PRD.md §4: every change to a limit writes an audit row with a timestamp
 * and the previous value. The point of the log is the sentence "when a bad
 * month happens, you need to know whether a limit moved first", which only
 * works if the row records what it moved *from*. */
describe('setRiskLimit', () => {
  it('changes the ceiling and logs the previous value', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setRiskLimit('max_risk_per_trade_pct', 10)

    const s = useUIStore.getState()
    expect(s.riskLimits.find((l) => l.key === 'max_risk_per_trade_pct')!.value).toBe(10)

    expect(s.auditLog).toHaveLength(before + 1)
    expect(s.auditLog[0]).toMatchObject({
      category: 'risk',
      field: 'max_risk_per_trade_pct',
      previousValue: '7',
      newValue: '10',
    })
  })

  it('writes exactly one row per change', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setRiskLimit('max_daily_loss_pct', 15)
    expect(useUIStore.getState().auditLog).toHaveLength(before + 1)
  })

  it('logs nothing when the value did not actually change', () => {
    const before = useUIStore.getState().auditLog.length
    const current = RISK_LIMITS.find((l) => l.key === 'max_daily_loss_pct')!.value
    useUIStore.getState().setRiskLimit('max_daily_loss_pct', current)

    expect(useUIStore.getState().auditLog).toHaveLength(before)
  })

  it('refuses a value outside the range and logs nothing', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setRiskLimit('max_risk_per_trade_pct', 500)

    const s = useUIStore.getState()
    expect(s.riskLimits.find((l) => l.key === 'max_risk_per_trade_pct')!.value).toBe(7)
    expect(s.auditLog).toHaveLength(before)
  })

  it('refuses a fractional position count', () => {
    useUIStore.getState().setRiskLimit('max_concurrent_positions', 8.5)
    expect(
      useUIStore.getState().riskLimits.find((l) => l.key === 'max_concurrent_positions')!.value,
    ).toBe(8)
  })

  it('leaves the other four limits alone', () => {
    useUIStore.getState().setRiskLimit('max_risk_per_trade_pct', 10)

    for (const limit of RISK_LIMITS.filter((l) => l.key !== 'max_risk_per_trade_pct')) {
      expect(useUIStore.getState().riskLimits.find((l) => l.key === limit.key)!.value).toBe(
        limit.value,
      )
    }
  })
})

describe('setNotificationRoute', () => {
  it('toggles one channel of one event and logs it', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setNotificationRoute('order_filled', 'bell', false)

    const s = useUIStore.getState()
    const route = s.notificationRoutes.find((r) => r.event === 'order_filled')!
    expect(route.bell).toBe(false)
    // The other channel of the same event is untouched.
    expect(route.discord).toBe(true)

    expect(s.auditLog).toHaveLength(before + 1)
    expect(s.auditLog[0]).toMatchObject({
      category: 'notification',
      field: notificationAuditField('order_filled', 'bell'),
      previousValue: 'on',
      newValue: 'off',
    })
  })

  it('logs nothing when the cell is already in the requested state', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setNotificationRoute('order_filled', 'bell', true)
    expect(useUIStore.getState().auditLog).toHaveLength(before)
  })

  /** Every cell is editable, bell included — PRD.md §10's table is the
   * shipped default rather than an invariant. The confirm that guards
   * silencing a critical event is a UI concern; the store does not veto it. */
  it('allows a critical event to be silenced, since the confirm lives in the UI', () => {
    useUIStore.getState().setNotificationRoute('engine_error', 'bell', false)
    useUIStore.getState().setNotificationRoute('engine_error', 'discord', false)

    const route = useUIStore.getState().notificationRoutes.find((r) => r.event === 'engine_error')!
    expect(route.bell).toBe(false)
    expect(route.discord).toBe(false)
  })
})

describe('setDataFeed', () => {
  it('changes a feed and logs the previous value', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setDataFeed('stockHistorical', 'iex')

    const s = useUIStore.getState()
    expect(s.dataFeeds.find((f) => f.key === 'stockHistorical')!.value).toBe('iex')
    expect(s.auditLog).toHaveLength(before + 1)
    expect(s.auditLog[0]).toMatchObject({
      category: 'feed',
      field: 'stockHistorical',
      previousValue: 'sip',
      newValue: 'iex',
    })
  })

  /** Requesting OPRA on Basic returns an auth error, not empty data. Storing
   * it would break every subsequent options request for nothing. */
  it('refuses a feed the current plan cannot serve', () => {
    const before = useUIStore.getState().auditLog.length
    useUIStore.getState().setDataFeed('options', 'opra')

    const s = useUIStore.getState()
    expect(s.dataFeeds.find((f) => f.key === 'options')!.value).toBe('indicative')
    expect(s.auditLog).toHaveLength(before)
  })

  it('refuses a value that is not an option for that feed at all', () => {
    useUIStore.getState().setDataFeed('options', 'nonsense')
    expect(useUIStore.getState().dataFeeds.find((f) => f.key === 'options')!.value).toBe(
      'indicative',
    )
  })
})

describe('markNotificationsRead', () => {
  it('clears the unread count for the book on screen', () => {
    expect(unreadCount(useUIStore.getState().notifications, 'paper')).toBeGreaterThan(0)
    useUIStore.getState().markNotificationsRead()
    expect(unreadCount(useUIStore.getState().notifications, 'paper')).toBe(0)
  })

  /** Opening the bell in Paper must not mark a Cash notification read. You
   * have never seen it — it is not in the panel you just opened. */
  it('leaves the other account notifications unread', () => {
    const cashUnread = NOTIFICATIONS.filter((n) => n.account === 'cash' && !n.read)
    expect(cashUnread.length).toBeGreaterThan(0)

    useUIStore.getState().markNotificationsRead()

    const after = useUIStore.getState().notifications
    for (const n of cashUnread) {
      expect(after.find((x) => x.id === n.id)!.read).toBe(false)
    }
  })

  it('marks account-less events read, since those were on screen', () => {
    useUIStore.setState({
      notifications: NOTIFICATIONS.map((n) => (n.account === null ? { ...n, read: false } : n)),
    })
    useUIStore.getState().markNotificationsRead()

    for (const n of useUIStore.getState().notifications.filter((x) => x.account === null)) {
      expect(n.read).toBe(true)
    }
  })
})

describe('dismissNotification', () => {
  it('removes one notification and leaves the rest', () => {
    const target = NOTIFICATIONS[0]
    useUIStore.getState().dismissNotification(target.id)

    const after = useUIStore.getState().notifications
    expect(after.some((n) => n.id === target.id)).toBe(false)
    expect(after).toHaveLength(NOTIFICATIONS.length - 1)
  })
})

/** The bell reports what the mock broker did on its own. These tests are
 * about the *gate*: routing decides what is delivered, and it decides it at
 * emission — never by hiding history after the fact. */
describe('notifications emitted by the tick', () => {
  const restingSell = (positionId: string, contract: string, quantity: number) => ({
    id: 'wo-notify',
    positionId,
    contractKey: null,
    contract,
    side: 'STC' as const,
    orderType: 'limit' as const,
    quantity,
    limitPrice: 0.01,
    stopPrice: null,
    timeInForce: 'gtc' as const,
    placedAt: '2026-08-07T15:00:00Z',
    activityId: 'act-1',
  })

  it('announces a working order that filled', () => {
    const target = PAPER.positions.find((p) => p.id === 'pos-2')!
    useUIStore.setState({
      workingOrders: {
        paper: [restingSell(target.id, `${target.symbol} ${target.contract}`, target.quantity)],
        cash: [],
      },
    })

    const before = useUIStore.getState().notifications.length
    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.notifications.length).toBe(before + 1)
    expect(s.notifications[0].event).toBe('order_filled')
    expect(s.notifications[0].account).toBe('paper')
    expect(s.notifications[0].read).toBe(false)
  })

  /** The important one. Unchecking the bell suppresses the *notification*
   * and nothing else — the order still fills and the ledger still records
   * it. A routing preference that quietly stopped orders from filling would
   * be catastrophic and completely invisible. */
  it('suppresses the bell entry when the route is off, but still fills the order', () => {
    const target = PAPER.positions.find((p) => p.id === 'pos-2')!
    useUIStore.getState().setNotificationRoute('order_filled', 'bell', false)
    useUIStore.setState({
      workingOrders: {
        paper: [restingSell(target.id, `${target.symbol} ${target.contract}`, target.quantity)],
        cash: [],
      },
    })

    const before = useUIStore.getState().notifications.length
    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.notifications.length).toBe(before)
    // The fill itself happened regardless.
    expect(s.workingOrders.paper.some((o) => o.id === 'wo-notify')).toBe(false)
    expect(s.openPositions.paper.some((p) => p.id === target.id)).toBe(false)
  })

  it('does not erase notifications already received when a route is turned off', () => {
    const before = useUIStore.getState().notifications.length
    useUIStore.getState().setNotificationRoute('order_filled', 'bell', false)
    expect(useUIStore.getState().notifications).toHaveLength(before)
  })

  /** A take-profit is a fill and a stop is a stop. They route separately and
   * they read differently — a stop is a `warning`, a rejection is an
   * `error`, and neither is the other. */
  it('reports a triggered stop as stop_loss_hit, not as a fill', () => {
    const target = PAPER.positions.find((p) => p.direction === 'long')!
    useUIStore.getState().upsertExit(target.id, {
      // Take-profit unreachably high and the stop just under it, so the mark
      // is below both and only the stop can trigger.
      takeProfit: 9_999,
      stopPrice: 9_998,
      stopLimitPrice: null,
      timeInForce: 'gtc',
      heldBy: 'broker',
    })
    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.notifications[0].event).toBe('stop_loss_hit')
    expect(s.notifications[0].detail).toContain('stop loss')
  })

  it('reports a triggered take-profit as a fill', () => {
    const target = PAPER.positions.find((p) => p.direction === 'long')!
    useUIStore.getState().upsertExit(target.id, {
      takeProfit: 0.01,
      stopPrice: 0.001,
      stopLimitPrice: null,
      timeInForce: 'gtc',
      heldBy: 'broker',
    })
    useUIStore.getState().tick()

    const s = useUIStore.getState()
    expect(s.notifications[0].event).toBe('order_filled')
    expect(s.notifications[0].detail).toContain('take profit')
  })

  it('emits nothing on a quiet tick', () => {
    const before = useUIStore.getState().notifications.length
    useUIStore.getState().tick()
    expect(useUIStore.getState().notifications).toHaveLength(before)
  })

  /** You were on screen when you clicked these. Activity records them; a
   * notification about your own click is what teaches you to ignore the
   * bell. */
  it('emits nothing for a manual halt or flatten', () => {
    const before = useUIStore.getState().notifications.length
    useUIStore.getState().halt()
    useUIStore.getState().flatten()
    expect(useUIStore.getState().notifications).toHaveLength(before)
  })

  it('scopes an emitted notification to the account that produced it', () => {
    useUIStore.setState({ accountMode: 'cash' })
    const target = CASH.positions.find((p) => p.direction === 'long')!
    useUIStore.getState().upsertExit(target.id, {
      takeProfit: 0.01,
      stopPrice: 0.001,
      stopLimitPrice: null,
      timeInForce: 'gtc',
      heldBy: 'broker',
    })
    useUIStore.getState().tick()

    const emitted = useUIStore.getState().notifications[0]
    expect(emitted.account).toBe('cash')
    // And it does not appear in the paper book.
    expect(
      visibleNotifications(useUIStore.getState().notifications, 'paper').map((n) => n.id),
    ).not.toContain(emitted.id)
  })
})


describe('chat history', () => {
  beforeEach(() => {
    useUIStore.setState({ ...initialState }, true)
  })

  it('opens with an empty transcript and a seeded history beside it', () => {
    const s = useUIStore.getState()
    expect(s.chat).toEqual([])
    expect(s.chatHistory).toEqual(CHAT_HISTORY)
  })

  /** Nothing to file, and you are already in a new chat. */
  it('newChat is a no-op on an untouched transcript', () => {
    useUIStore.getState().newChat()

    expect(useUIStore.getState().chat).toEqual([])
    expect(useUIStore.getState().chatHistory).toHaveLength(CHAT_HISTORY.length)
  })

  it('newChat files the transcript newest-first and clears the chat', () => {
    useUIStore.getState().sendChatMessage('How is my risk configured?')
    expect(useUIStore.getState().chat.length).toBeGreaterThan(0)

    useUIStore.getState().newChat()

    const s = useUIStore.getState()
    expect(s.chat).toEqual([])
    expect(s.chatHistory).toHaveLength(CHAT_HISTORY.length + 1)
    expect(s.chatHistory[0].title).toBe('How is my risk configured?')
  })

  it('openChat loads an archived conversation and takes it out of the history', () => {
    const target = CHAT_HISTORY[0]

    useUIStore.getState().openChat(target.id)

    const s = useUIStore.getState()
    expect(s.chat).toEqual(target.messages)
    expect(s.chatHistory.map((c) => c.id)).not.toContain(target.id)
    expect(s.chatHistory).toHaveLength(CHAT_HISTORY.length - 1)
  })

  /** Switching away must never discard what is on screen. */
  it('openChat files the current transcript on the way out', () => {
    useUIStore.getState().sendChatMessage('What are today’s recommendations?')
    useUIStore.getState().openChat(CHAT_HISTORY[1].id)

    const s = useUIStore.getState()
    expect(s.chat).toEqual(CHAT_HISTORY[1].messages)
    expect(s.chatHistory[0].title).toBe('What are today’s recommendations?')
    // One left, one arrived.
    expect(s.chatHistory).toHaveLength(CHAT_HISTORY.length)
  })

  it('ignores an unknown conversation id rather than clearing the chat', () => {
    useUIStore.getState().sendChatMessage('How is my risk configured?')
    const before = useUIStore.getState().chat

    useUIStore.getState().openChat('chat-does-not-exist')

    expect(useUIStore.getState().chat).toEqual(before)
    expect(useUIStore.getState().chatHistory).toHaveLength(CHAT_HISTORY.length)
  })

  /** A conversation is either current or archived, never both — otherwise
   * the two copies have to agree on every keystroke. */
  it('never holds the live transcript in the history as well', () => {
    useUIStore.getState().openChat(CHAT_HISTORY[0].id)

    const s = useUIStore.getState()
    const archivedIds = new Set(s.chatHistory.flatMap((c) => c.messages.map((m) => m.id)))
    for (const m of s.chat) expect(archivedIds.has(m.id)).toBe(false)
  })
})
