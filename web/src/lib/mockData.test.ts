import { describe, it, expect } from 'vitest'
import { ACCOUNT_SNAPSHOTS, BENCHMARK_HISTORY, OPEN_POSITIONS, STRATEGIES } from './mockData'

/** The fixtures are generated from a seeded PRNG so screenshots and tests
 * don't flake between runs (CLAUDE.md, mockData.ts). "Seeded" only holds if
 * something notices when the draw order changes — inserting a series that
 * shares the module-level stream silently shifts every series after it. */
describe('generated series are deterministic', () => {
  it('pins the paper account to its known values', () => {
    const h = ACCOUNT_SNAPSHOTS.paper.portfolioHistory
    expect(h).toHaveLength(261)
    expect(h[0]).toEqual({ date: '2025-08-08', value: 24_969.91 })
    expect(h[h.length - 1]).toEqual({ date: '2026-08-07', value: 32_899.85 })
  })

  it('pins the cash account to its known values', () => {
    const h = ACCOUNT_SNAPSHOTS.cash.portfolioHistory
    expect(h).toHaveLength(261)
    expect(h[0]).toEqual({ date: '2025-08-08', value: 8_012.3 })
    expect(h[h.length - 1]).toEqual({ date: '2026-08-07', value: 9_712.41 })
  })

  it('runs the benchmark over the same dates as the portfolio', () => {
    const paper = ACCOUNT_SNAPSHOTS.paper.portfolioHistory
    expect(BENCHMARK_HISTORY).toHaveLength(paper.length)
    expect(BENCHMARK_HISTORY[0].date).toBe(paper[0].date)
  })

  it('never emits a weekend close', () => {
    for (const p of ACCOUNT_SNAPSHOTS.cash.portfolioHistory) {
      const day = new Date(`${p.date}T00:00:00Z`).getUTCDay()
      expect(day).not.toBe(0)
      expect(day).not.toBe(6)
    }
  })
})

describe('paper and cash are genuinely different accounts', () => {
  it('holds different money in each', () => {
    const paper = ACCOUNT_SNAPSHOTS.paper
    const cash = ACCOUNT_SNAPSHOTS.cash
    const last = (h: { value: number }[]) => h[h.length - 1].value

    expect(last(cash.portfolioHistory)).not.toBe(last(paper.portfolioHistory))
    expect(cash.volume24h).not.toBe(paper.volume24h)
  })

  it('covers a falling balance as well as a rising one', () => {
    // A trend card that has only ever been rendered green is a state nobody
    // has actually looked at.
    expect(ACCOUNT_SNAPSHOTS.paper.balanceTrend.changePct).toBeGreaterThan(0)
    expect(ACCOUNT_SNAPSHOTS.cash.balanceTrend.changePct).toBeLessThan(0)
  })
})

describe('fixtures cover the states the Dashboard can render', () => {
  it('includes a strategy that has never traded live', () => {
    expect(STRATEGIES.some((s) => s.live === null)).toBe(true)
  })

  it('includes both a long and a short position, so Flatten has both close paths', () => {
    expect(OPEN_POSITIONS.some((p) => p.direction === 'long')).toBe(true)
    expect(OPEN_POSITIONS.some((p) => p.direction === 'short')).toBe(true)
  })
})
