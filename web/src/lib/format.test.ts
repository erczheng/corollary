import { describe, it, expect } from 'vitest'
import {
  formatDateET,
  formatDateOnly,
  formatExpiry,
  formatMarketCap,
  formatSessionDateTimeET,
  formatSessionDay,
  formatSignedNumber,
} from './format'

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

/** Names the session a chart is actually showing. Same UTC rule as
 * formatDateOnly — it takes a calendar date, and an instant has to be
 * resolved to its market day (api.ts#latestSession) before it gets here. */
describe('formatSessionDay', () => {
  it('names the weekday, which is the whole reason the label helps', () => {
    // "Fri" is what says the two days since were a weekend.
    expect(formatSessionDay('2026-09-11')).toBe('Fri, Sep 11')
  })

  it('does not slip to the previous day the way an ET render would', () => {
    // ET is behind UTC, so 2026-09-11 through an ET formatter is Sep 10 —
    // a Friday session labelled Thursday.
    expect(formatSessionDay('2026-09-11')).not.toContain('Thu')
    expect(formatSessionDay('2026-09-11')).not.toContain('Sep 10')
  })

  it('omits the year, which the newest point on a live chart never needs', () => {
    expect(formatSessionDay('2026-01-02')).toBe('Fri, Jan 2')
  })
})

/** The crosshair label for an intraday chart, which is drawn on an ordinal
 * axis: the sessions are concatenated, so x is a position and no longer
 * says when a point was. This is the only surface that can, so it carries
 * the weekday, the date, the year *and* the time.
 *
 * The opposite timestamp rule from `formatSessionDay` above, because the
 * input is the opposite kind of value: `IntradayPoint.at` is a UTC
 * **instant**, not a calendar date, and every instant in this terminal is
 * shown in America/New_York. */
describe('formatSessionDateTimeET', () => {
  it('carries the date and the time, not one or the other', () => {
    // 19:55Z is 3:55 PM ET on Friday the 11th — the last five-minute bar
    // of a regular session.
    const label = formatSessionDateTimeET('2026-09-11T19:55:00Z')
    expect(label).toContain('Fri, Sep 11, 2026')
    expect(label).toContain('3:55 PM')
  })

  it('renders in market time, so a late bar keeps its own day', () => {
    // 20:00Z is 4:00 PM ET on the 11th. The UTC day is already the 11th
    // here, but an evening instant is where the two part company: a bare
    // ET-less render of the next case would name the 12th.
    expect(formatSessionDateTimeET('2026-09-12T00:30:00Z')).toContain('Fri, Sep 11, 2026')
    expect(formatSessionDateTimeET('2026-09-12T00:30:00Z')).toContain('8:30 PM')
  })

  it('names the weekday, which is what makes a concatenated week readable', () => {
    // Monday 09:30 ET, the point that sits immediately right of Friday's
    // close on the ordinal axis. Without the weekday the two are
    // indistinguishable.
    expect(formatSessionDateTimeET('2026-09-14T13:30:00Z')).toContain('Mon, Sep 14, 2026')
    expect(formatSessionDateTimeET('2026-09-14T13:30:00Z')).toContain('9:30 AM')
  })
})

/** The wire carries market cap in **dollars** -- the server scales Finnhub's
 * millions with an exact Decimal. Read as billions, NVDA's $5.43T printed as
 * "$5434790884.05T". These are the live values that exposed it. */
describe('formatMarketCap', () => {
  it('formats a dollar figure in trillions', () => {
    expect(formatMarketCap(5_434_790_884_052)).toBe('$5.43T')
    expect(formatMarketCap(4_918_530_438_281)).toBe('$4.92T')
  })

  it('formats billions and millions', () => {
    expect(formatMarketCap(23_285_494_048)).toBe('$23.29B')
    expect(formatMarketCap(850_000_000)).toBe('$850.00M')
  })

  it('renders a fund as an em dash, never $0', () => {
    expect(formatMarketCap(null)).toBe('—')
  })
})
