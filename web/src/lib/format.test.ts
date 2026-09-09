import { describe, it, expect } from 'vitest'
import { formatDateET, formatDateOnly, formatExpiry, formatSignedNumber } from './format'

/** A bare 'YYYY-MM-DD' is a calendar date, not an instant. It parses as UTC
 * midnight, and ET is behind UTC, so rendering one in ET shows the previous
 * day. The chart's "Since ..." caption did exactly that: the series starts
 * 2025-08-08 and the caption read Aug 7, 2025. */
describe('formatDateOnly', () => {
  it('renders the calendar date it was given, not the day before', () => {
    expect(formatDateOnly('2025-08-08')).toBe('Aug 8, 2025')
  })

  it('is the fix for a real off-by-one — formatDateET on the same input is a day early', () => {
    expect(formatDateET('2025-08-08T00:00:00Z')).toBe('Aug 7, 2025')
    expect(formatDateOnly('2025-08-08')).toBe('Aug 8, 2025')
  })

  it('holds either side of a DST boundary', () => {
    // ET is UTC-5 in January and UTC-4 in July; both are behind UTC, so
    // both would slip a day under ET rendering.
    expect(formatDateOnly('2026-01-01')).toBe('Jan 1, 2026')
    expect(formatDateOnly('2026-07-01')).toBe('Jul 1, 2026')
  })

  it('holds across a year boundary, where the slip would change the year', () => {
    expect(formatDateOnly('2026-01-01')).toBe('Jan 1, 2026')
    expect(formatDateET('2026-01-01T00:00:00Z')).toBe('Dec 31, 2025')
  })

  it('agrees with formatExpiry, which drops only the year', () => {
    expect(formatDateOnly('2026-11-21')).toBe('Nov 21, 2026')
    expect(formatExpiry('2026-11-21')).toBe('Nov 21')
  })
})


describe('formatSignedNumber', () => {
  it('signs a rise and a fall, with no currency or percent', () => {
    expect(formatSignedNumber(0.45)).toBe('+0.45')
    expect(formatSignedNumber(-0.45)).toBe('−0.45')
  })

  /** The same U+2212 the money and percent formatters use, not a hyphen —
   * otherwise a column of signed values built from two helpers would not
   * line up. */
  it('uses the same minus sign as the other formatters', () => {
    expect(formatSignedNumber(-1).startsWith('−')).toBe(true)
    expect(formatSignedNumber(-1).startsWith('-')).toBe(false)
  })

  it('leaves zero unsigned', () => {
    expect(formatSignedNumber(0)).toBe('0.00')
  })

  it('takes a digit count', () => {
    expect(formatSignedNumber(-2.605, 1)).toBe('−2.6')
    expect(formatSignedNumber(3, 0)).toBe('+3')
  })
})
