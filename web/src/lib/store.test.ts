import { describe, it, expect, beforeEach } from 'vitest'
import { useUIStore } from './store'
import { OPEN_POSITIONS, RECENT_ACTIVITY } from './mockData'

const initialState = useUIStore.getState()

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
    expect(s.openPositions).toHaveLength(OPEN_POSITIONS.length)
    expect(s.activity).toHaveLength(RECENT_ACTIVITY.length)
  })

  it('flatten closes every position, then halts', () => {
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.openPositions).toHaveLength(0)
    expect(s.isHalted).toBe(true)
  })

  it('halt and flatten do not have the same effect', () => {
    useUIStore.getState().halt()
    const afterHalt = useUIStore.getState().openPositions.length

    useUIStore.setState(initialState, true)
    useUIStore.getState().flatten()
    const afterFlatten = useUIStore.getState().openPositions.length

    expect(afterHalt).not.toBe(afterFlatten)
  })

  it('resume lifts a halt without reopening anything', () => {
    useUIStore.getState().flatten()
    useUIStore.getState().resume()

    const s = useUIStore.getState()
    expect(s.isHalted).toBe(false)
    expect(s.openPositions).toHaveLength(0)
  })
})

describe('flatten executions', () => {
  it('records one execution per closed position, newest first', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    expect(activity).toHaveLength(RECENT_ACTIVITY.length + OPEN_POSITIONS.length)
    expect(activity.slice(0, OPEN_POSITIONS.length).every((a) => a.status === 'filled')).toBe(true)
  })

  it('sells a long to close at the bid and buys a short to close at the ask', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    const long = OPEN_POSITIONS.find((p) => p.direction === 'long')!
    const short = OPEN_POSITIONS.find((p) => p.direction === 'short')!

    const longClose = activity.find((a) => a.id === `act-flat-${long.id}`)!
    const shortClose = activity.find((a) => a.id === `act-flat-${short.id}`)!

    expect(longClose.action).toBe('STC')
    expect(longClose.price).toBe(long.bid)
    expect(shortClose.action).toBe('BTC')
    expect(shortClose.price).toBe(short.ask)
  })

  it('carries each position P&L onto its closing execution', () => {
    useUIStore.getState().flatten()

    const { activity } = useUIStore.getState()
    for (const p of OPEN_POSITIONS) {
      expect(activity.find((a) => a.id === `act-flat-${p.id}`)!.pnl).toBe(p.pnl)
    }
  })

  it('flattening an already-empty book halts without inventing executions', () => {
    useUIStore.getState().flatten()
    const afterFirst = useUIStore.getState().activity.length

    useUIStore.getState().resume()
    useUIStore.getState().flatten()

    const s = useUIStore.getState()
    expect(s.activity).toHaveLength(afterFirst)
    expect(s.isHalted).toBe(true)
  })
})
