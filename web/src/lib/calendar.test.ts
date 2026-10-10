import { describe, it, expect } from 'vitest'
import {
  addDays,
  calendarWindow,
  economicFigures,
  emptyReading,
  etClock,
  etInstant,
  groupByType,
  jobTone,
  removeConsequence,
  timeSlot,
  uncoveredAfter,
} from './calendar'
import { formatDecimalFigure, formatPriceRange } from './format'
import type { CalendarEvent, CalendarJobNotice, CalendarRange } from './types'

function event(overrides: Partial<CalendarEvent> = {}): CalendarEvent {
  return {
    id: '1',
    date: '2026-10-14',
    at: null,
    type: 'geopolitical',
    title: 'G20 summit',
    ticker: null,
    source: 'manual',
    editable: true,
    session: null,
    estimate: null,
    prior: null,
    actual: null,
    consensus: null,
    unit: null,
    exchange: null,
    shares: null,
    priceLow: null,
    priceHigh: null,
    ipoStatus: null,
    ...overrides,
  }
}

function job(overrides: Partial<CalendarJobNotice> = {}): CalendarJobNotice {
  return {
    job: 'calendar_earnings',
    kinds: ['earnings'],
    state: 'ok',
    accessDenied: false,
    lastSuccess: '2026-10-12T11:00:00Z',
    lastFailure: null,
    lastErrorType: null,
    lastSkipped: null,
    lastSkipReason: null,
    freshAsOf: null,
    nextRun: '2026-10-13T11:00:00Z',
    coveredThrough: '2026-11-08',
    rowsInRange: 0,
    message: 'Earnings: last fetched Oct 12, 7:00 AM.',
    ...overrides,
  }
}

function range(overrides: Partial<CalendarRange['notices']> = {}): CalendarRange {
  return {
    start: '2026-10-12',
    end: '2026-11-08',
    maxSpanDays: 92,
    days: [],
    total: 0,
    notices: {
      seedGaps: [],
      jobs: [job()],
      releaseFigures: { state: 'pending_owner_decision', reason: 'pending' },
      ...overrides,
    },
  }
}

describe('the window', () => {
  it('runs four weeks forward from Eastern today, inclusive', () => {
    expect(calendarWindow('2026-10-12')).toEqual({ from: '2026-10-12', to: '2026-11-08' })
  })

  it('adds calendar days in UTC across a DST change', () => {
    expect(addDays('2026-10-31', 2)).toBe('2026-11-02')
  })
})

describe('timeSlot', () => {
  /** Decision 7: a vendor's `hour` is a session, never a time. */
  it('reads an earnings session, never a time — even with an instant present', () => {
    expect(timeSlot(event({ type: 'earnings', session: 'amc', at: '2026-10-14T20:05:00Z' }))).toEqual(
      { kind: 'session', text: 'After close' },
    )
    expect(timeSlot(event({ type: 'earnings', session: 'bmo' })).kind).toBe('session')
    expect(timeSlot(event({ type: 'earnings', session: 'dmh' }))).toMatchObject({
      text: 'During market hours',
    })
  })

  it('labels a date-only BoJ or BoE decision as the bank’s own date', () => {
    const boj = timeSlot(event({ type: 'central-bank', title: 'BoJ policy decision', source: 'seed' }))
    expect(boj).toMatchObject({ kind: 'local-date', text: 'Tokyo date' })
    const boe = timeSlot(event({ type: 'central-bank', title: 'BoE policy decision', source: 'seed' }))
    expect(boe).toMatchObject({ kind: 'local-date', text: 'London date' })
  })

  it('reads a timed central-bank row as a time, and other date-only rows as All day', () => {
    expect(
      timeSlot(event({ type: 'central-bank', title: 'FOMC policy decision', at: '2026-10-28T18:00:00Z' })),
    ).toEqual({ kind: 'time', at: '2026-10-28T18:00:00Z' })
    expect(timeSlot(event({ type: 'dividend' }))).toEqual({ kind: 'all-day', text: 'All day' })
  })
})

describe('figures', () => {
  it('formats decimal strings verbatim, never through a float', () => {
    expect(formatDecimalFigure('1.2350')).toBe('1.2350')
    expect(formatDecimalFigure('3.10', '%')).toBe('3.10%')
    expect(formatDecimalFigure('227', 'K')).toBe('227 K')
    expect(formatPriceRange('14.00', '16.00')).toBe('$14.00–$16.00')
    expect(formatPriceRange('15.50', '15.50')).toBe('$15.50')
    expect(formatPriceRange(null, null)).toBeNull()
  })

  it('reports pending economic figures while the owner decision is open', () => {
    const e = event({ type: 'economic', consensus: 'unavailable' })
    expect(economicFigures(e, range())).toEqual({ kind: 'pending' })
    expect(economicFigures({ ...e, prior: '3.1', unit: '%' }, range())).toEqual({
      kind: 'figures',
      prior: '3.1',
      actual: null,
      unit: '%',
    })
  })
})

describe('notices', () => {
  it('reads failing as error, access_denied as caution, the rest calmly', () => {
    expect(jobTone('failing')).toBe('error')
    expect(jobTone('access_denied')).toBe('caution')
    expect(jobTone('fresh_at_start')).toBe('calm')
  })

  it('finds where a job stops covering the range', () => {
    expect(uncoveredAfter(job({ coveredThrough: '2026-11-02' }), '2026-11-08')).toBe('2026-11-02')
    expect(uncoveredAfter(job({ coveredThrough: '2026-11-08' }), '2026-11-08')).toBeNull()
    expect(uncoveredAfter(job({ coveredThrough: null }), '2026-11-08')).toBe('all')
  })

  /** Only a fully covered, fully run calendar may say "nothing scheduled". */
  it('reads an empty range three ways', () => {
    expect(emptyReading(range())).toBe('quiet')
    expect(emptyReading(range({ jobs: [job({ state: 'never_run', coveredThrough: null })] }))).toBe(
      'incomplete',
    )
    expect(emptyReading(range({ jobs: [job({ coveredThrough: '2026-11-02' })] }))).toBe('incomplete')
    expect(
      emptyReading(
        range({
          seedGaps: [{ bank: 'ECB', year: 2027, kind: 'unpublished', reason: 'x', coversFrom: null }],
        }),
      ),
    ).toBe('incomplete')
    expect(emptyReading(range({ jobs: [job({ state: 'failing' })] }))).toBe('failing')
    expect(emptyReading(range({ jobs: [job({ state: 'access_denied' })] }))).toBe('failing')
  })
})

describe('the manual form’s instant', () => {
  it('builds an ISO instant with the ET offset in force on that date', () => {
    expect(etInstant('2026-10-16', '09:30')).toBe('2026-10-16T09:30:00-04:00')
    expect(etInstant('2026-11-16', '09:30')).toBe('2026-11-16T09:30:00-05:00')
    expect(etInstant('2026-10-16', '')).toBeNull()
  })

  it('reads an instant back as the ET wall clock', () => {
    expect(etClock('2026-10-16T13:30:00Z')).toBe('09:30')
    expect(etClock('2026-10-15T00:30:00Z')).toBe('20:30')
  })

  it('states the removal consequence concretely', () => {
    expect(removeConsequence({ title: 'G20 summit', date: '2026-10-16' })).toMatch(
      /^Remove 'G20 summit' on Fri, Oct 16 from the calendar\?/,
    )
  })
})

describe('groupByType', () => {
  it('orders groups and does not mutate', () => {
    const events = [event({ id: 'a' }), event({ id: 'b', type: 'ipo' }), event({ id: 'c', type: 'economic' })]
    const before = JSON.stringify(events)
    expect(groupByType(events).map(([t]) => t)).toEqual(['economic', 'ipo', 'geopolitical'])
    expect(JSON.stringify(events)).toBe(before)
  })
})
