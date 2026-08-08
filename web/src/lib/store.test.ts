import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from './store'
import { ACCOUNT_SNAPSHOTS } from './mockData'

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
describe('closePosition', () => {
  it('removes just that position and leaves the rest of the book open', () => {
    const target = PAPER.positions[1]
    useUIStore.getState().closePosition(target.id)

    const s = useUIStore.getState()
    expect(s.openPositions.paper).toHaveLength(PAPER.positions.length - 1)
    expect(s.openPositions.paper.some((p) => p.id === target.id)).toBe(false)
  })

  it('does not halt the engine', () => {
    useUIStore.getState().closePosition(PAPER.positions[0].id)

    expect(useUIStore.getState().isHalted).toBe(false)
  })

  it('logs the close as an execution, priced on the side it crossed', () => {
    const short = PAPER.positions.find((p) => p.direction === 'short')!
    useUIStore.getState().closePosition(short.id)

    const close = useUIStore.getState().activity.paper[0]
    expect(close.id).toBe(`act-close-${short.id}`)
    expect(close.action).toBe('BTC')
    expect(close.price).toBe(short.ask)
    expect(close.status).toBe('filled')
  })

  it('distinguishes a single close from a flatten in the audit trail', () => {
    useUIStore.getState().closePosition(PAPER.positions[0].id)
    useUIStore.getState().flatten()

    const ids = useUIStore.getState().activity.paper.map((a) => a.id)
    expect(ids).toContain(`act-close-${PAPER.positions[0].id}`)
    expect(ids).toContain(`act-flat-${PAPER.positions[1].id}`)
  })

  it('ignores an id that is not in the active account', () => {
    const before = useUIStore.getState()
    useUIStore.getState().closePosition(CASH.positions[0].id)

    const after = useUIStore.getState()
    expect(after.openPositions.paper).toEqual(before.openPositions.paper)
    expect(after.activity.paper).toEqual(before.activity.paper)
  })
})
