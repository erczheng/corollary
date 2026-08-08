import { describe, it, expect } from 'vitest'
import { rangeChange } from './chart'
import type { PricePoint } from './mockData'

const series: PricePoint[] = [
  { date: '2026-01-05', value: 1000 },
  { date: '2026-01-06', value: 1100 },
  { date: '2026-01-07', value: 900 },
  { date: '2026-01-08', value: 1250 },
]

describe('rangeChange', () => {
  it('measures from the earlier point to the later one', () => {
    const r = rangeChange(series, 0, 3)!

    expect(r.from.date).toBe('2026-01-05')
    expect(r.to.date).toBe('2026-01-08')
    expect(r.change).toBe(250)
    expect(r.changePct).toBeCloseTo(25, 10)
  })

  it('reads the same window when the drag went right to left', () => {
    // Dragging backwards selects the same period; it is not a negative
    // window, and it must not invert the sign of the result.
    expect(rangeChange(series, 3, 0)).toEqual(rangeChange(series, 0, 3))
  })

  it('reports a loss as a loss', () => {
    const r = rangeChange(series, 1, 2)!

    expect(r.change).toBe(-200)
    expect(r.changePct).toBeCloseTo(-18.1818, 3)
  })

  it('returns nothing when the drag never left its starting point', () => {
    // Otherwise a stray click flashes "+$0.00", which looks like a
    // measurement rather than the absence of one.
    expect(rangeChange(series, 2, 2)).toBeNull()
  })

  it('returns nothing for indices outside the series', () => {
    expect(rangeChange(series, 0, 99)).toBeNull()
    expect(rangeChange(series, -1, 2)).toBeNull()
  })

  it('withholds the reading rather than dividing by a zero starting value', () => {
    const withZero: PricePoint[] = [
      { date: '2026-01-05', value: 0 },
      { date: '2026-01-06', value: 500 },
    ]
    expect(rangeChange(withZero, 0, 1)).toBeNull()
  })

  it('handles an empty series without throwing', () => {
    expect(rangeChange([], 0, 1)).toBeNull()
  })
})
