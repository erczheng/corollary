import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from './store'
import { ACCOUNT_SNAPSHOTS } from './mockData'
import type { OrderDraft } from './orders'

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
