import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MarketCalendar } from './MarketCalendar'
import type {
  CalendarEvent,
  CalendarJobNotice,
  CalendarRange,
  CalendarSeedGap,
} from '../lib/types'

/** Every response here is `corollary/api/schemas.py`'s `CalendarRange`,
 * camelCased as the API serves it, with figures as decimal strings. */

const TODAY = '2026-10-12'

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => (status === 204 ? Promise.reject(new Error('no body')) : Promise.resolve(body)),
  } as unknown as Response
}

function refusal(status: number, code: string, message: string): Response {
  return jsonResponse(status, { error: { code, message } })
}

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
    valueState: null,
    valueNote: null,
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
    rowsInRange: 1,
    message: 'Earnings: last fetched Oct 12, 7:00 AM.',
    ...overrides,
  }
}

const CPI_NOTE = "computed from FRED's levels: CPIAUCSL"
const LAPSE_NOTE = 'not published on FRED within 7 days'
const NAR_REFUSAL =
  'FRED refused EXHOSLUSM495S: the series is licensed by NAR and not served through the API.'

/** One economic row per `valueState`, plus a refused series, a negative
 * figure with a trailing zero, a comma'd unit label, and a non-economic row
 * whose value fields are all null — every state the row can render. */
function economicRow(overrides: Partial<CalendarEvent>): CalendarEvent {
  return event({
    type: 'economic',
    source: 'fred',
    editable: false,
    at: '2026-10-14T12:30:00Z',
    consensus: 'unavailable',
    ...overrides,
  })
}

const ECONOMIC_ROWS: CalendarEvent[] = [
  economicRow({
    id: 'cpi',
    title: 'Consumer Price Index',
    unit: '% m/m',
    valueState: 'filled',
    actual: '0.6',
    prior: '0.4',
    valueNote: CPI_NOTE,
  }),
  economicRow({
    id: 'trade',
    title: 'Trade balance',
    unit: '$B',
    valueState: 'filled',
    actual: '-105.0',
    prior: '-98.7',
    valueNote: "computed from FRED's levels: BOPGSTB",
  }),
  economicRow({
    id: 'ppi',
    title: 'Producer Price Index',
    unit: '% m/m, NSA',
    valueState: 'filled',
    actual: '0.3',
    prior: '-0.1',
    valueNote: "computed from FRED's levels: PPIFIS",
  }),
  economicRow({ id: 'retail', title: 'Retail sales', unit: '% m/m', valueState: 'awaiting' }),
  economicRow({
    id: 'claims',
    title: 'Initial jobless claims',
    unit: 'K',
    valueState: 'unavailable',
    valueNote: LAPSE_NOTE,
  }),
  economicRow({
    id: 'homes',
    title: 'Existing home sales',
    unit: 'M SAAR',
    valueState: 'unavailable',
    valueNote: NAR_REFUSAL,
  }),
  economicRow({
    id: 'payrolls',
    date: '2026-10-16',
    at: '2026-10-16T12:30:00Z',
    title: 'Nonfarm payrolls',
    unit: 'K',
    valueState: 'upcoming',
  }),
  event({ id: 'g20', date: '2026-10-16', title: 'G20 summit' }),
]

/** The `<li>` of the row titled `title`. */
function rowOf(scope: HTMLElement, title: string): HTMLElement {
  return within(scope).getByText(title).closest('li')!
}

function rangeOf(
  events: CalendarEvent[],
  notices: { jobs?: CalendarJobNotice[]; seedGaps?: CalendarSeedGap[] } = {},
): CalendarRange {
  const byDate = new Map<string, CalendarEvent[]>()
  for (const e of events) byDate.set(e.date, [...(byDate.get(e.date) ?? []), e])
  return {
    start: TODAY,
    end: '2026-11-08',
    maxSpanDays: 92,
    days: [...byDate.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, list]) => ({ date, events: list })),
    total: events.length,
    notices: {
      seedGaps: notices.seedGaps ?? [],
      jobs: notices.jobs ?? [job()],
    },
  }
}

interface Call {
  method: string
  path: string
  params: URLSearchParams
  body: unknown
}

let calls: Call[]
let served: () => Response | Promise<Response>
let writes: (call: Call) => Response

function stubFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: unknown, init?: RequestInit) => {
      const url = new URL(String(input), 'http://127.0.0.1')
      const call = {
        method: init?.method ?? 'GET',
        path: url.pathname,
        params: url.searchParams,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      }
      calls.push(call)
      if (call.method === 'GET' && call.path.endsWith('/calendar')) return Promise.resolve(served())
      return Promise.resolve(writes(call))
    }),
  )
}

let client: QueryClient

beforeEach(() => {
  calls = []
  served = () => jsonResponse(200, rangeOf([]))
  writes = () => jsonResponse(201, event())
  stubFetch()
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
})

function renderPanel() {
  return render(
    <QueryClientProvider client={client}>
      <MarketCalendar today={TODAY} />
    </QueryClientProvider>,
  )
}

function panel(): HTMLElement {
  return screen.getByRole('region', { name: 'Market calendar' })
}

async function day(label: string): Promise<HTMLElement> {
  return within(panel()).findByRole('group', { name: label })
}

describe('the request', () => {
  it('asks for four weeks of Eastern dates from today', async () => {
    renderPanel()
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    expect(calls[0].params.get('from')).toBe('2026-10-12')
    expect(calls[0].params.get('to')).toBe('2026-11-08')
  })
})

describe('states', () => {
  it('shows a designed skeleton while loading', () => {
    served = () => new Promise<Response>(() => {})
    renderPanel()
    expect(within(panel()).getByRole('status')).toHaveTextContent('Loading the market calendar')
  })

  it('reads a quiet, fully covered range as nothing scheduled', async () => {
    renderPanel()
    expect(await within(panel()).findByText(/^Nothing scheduled/)).toBeInTheDocument()
  })

  /** 08:00, before the day's first fetch in this process: not a quiet week. */
  it('never calls an unfetched range quiet', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([], {
          jobs: [
            job({
              state: 'never_run',
              lastSuccess: null,
              coveredThrough: null,
              message: 'Earnings: not fetched yet in this process; the first run is due Oct 12, 7:00 AM.',
            }),
          ],
        }),
      )
    renderPanel()
    expect(await within(panel()).findByText(/No events stored for/)).toBeInTheDocument()
    expect(within(panel()).queryByText(/Nothing scheduled/)).not.toBeInTheDocument()
    expect(within(panel()).getByText(/Earnings: Not fetched yet/)).toBeInTheDocument()
  })

  it('reads an empty range over a failing feed as failing, in error', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([], {
          jobs: [
            job({
              state: 'failing',
              lastFailure: '2026-10-12T11:00:00Z',
              lastErrorType: 'ProviderError',
              message: 'Earnings: stale since Oct 11, 7:00 AM; the last run failed at Oct 12, 7:00 AM (ProviderError) and runs again at its next slot.',
            }),
          ],
        }),
      )
    renderPanel()
    const empty = await within(panel()).findByText(/not a quiet calendar/)
    expect(empty).toHaveClass('text-error')
    expect(empty).not.toHaveClass('text-bearish')
    expect(within(panel()).getByText(/Earnings: Stale/)).toBeInTheDocument()
    expect(within(panel()).getByText(/stale since Oct 11, 7:00 AM/)).toBeInTheDocument()
  })

  it('keeps the calendar on screen when a refresh fails, and says it may be stale', async () => {
    served = () => jsonResponse(200, rangeOf([event()]))
    renderPanel()
    await day('Wed, Oct 14')
    served = () => refusal(503, 'service_unavailable', 'The calendar store is unavailable.')
    await client.refetchQueries({ queryKey: ['calendar'] })

    expect(await within(panel()).findByText('The calendar store is unavailable.')).toHaveClass(
      'text-error',
    )
    expect(within(panel()).getByText(/it may be stale/)).toBeInTheDocument()
    expect(within(panel()).getByText('G20 summit')).toBeInTheDocument()
  })

  it('reports a failed first read rather than an empty calendar', async () => {
    served = () => refusal(500, 'internal_error', 'The engine hit an internal error.')
    renderPanel()
    expect(await within(panel()).findByText('The engine hit an internal error.')).toBeInTheDocument()
    expect(within(panel()).queryByText(/Nothing scheduled/)).not.toBeInTheDocument()
  })

  it('carries no fixture marker', async () => {
    served = () => jsonResponse(200, rangeOf([event()]))
    renderPanel()
    await day('Wed, Oct 14')
    expect(within(panel()).queryByText(/Sample data/)).not.toBeInTheDocument()
  })
})

describe('rows', () => {
  it('groups by the served date — an event at 00:30Z shows under its ET date', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([
          event({
            id: '7',
            type: 'economic',
            source: 'fred',
            editable: false,
            title: 'Late release',
            date: '2026-10-14',
            at: '2026-10-15T00:30:00Z',
            consensus: 'unavailable',
            valueState: 'upcoming',
          }),
        ]),
      )
    renderPanel()
    const wed = await day('Wed, Oct 14')
    expect(within(wed).getByText('Late release')).toBeInTheDocument()
    expect(within(wed).getByText('8:30 PM')).toBeInTheDocument()
    expect(within(panel()).queryByRole('group', { name: 'Thu, Oct 15' })).not.toBeInTheDocument()
  })

  it('labels earnings by session and never prints a time', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([
          event({
            id: '2',
            type: 'earnings',
            source: 'finnhub',
            editable: false,
            ticker: 'AAPL',
            title: 'Apple Q4 earnings',
            session: 'amc',
            estimate: '1.2350',
          }),
          event({
            id: '3',
            type: 'earnings',
            source: 'finnhub',
            editable: false,
            ticker: 'JPM',
            title: 'JPMorgan Q3 earnings',
            session: 'bmo',
          }),
        ]),
      )
    renderPanel()
    const wed = await day('Wed, Oct 14')
    expect(within(wed).getByText('After close')).toBeInTheDocument()
    expect(within(wed).getByText('Before open')).toBeInTheDocument()
    expect(within(wed).queryByText(/\d{1,2}:\d{2}\s?(AM|PM)/)).not.toBeInTheDocument()
    // The estimate's digits verbatim, trailing zero included, in mono.
    const estimate = within(wed).getByText('1.2350')
    expect(estimate).toHaveClass('font-mono', 'tabular-nums')
  })

  it('shows an IPO’s exchange, price range, shares and status', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([
          event({
            id: '6',
            type: 'ipo',
            source: 'finnhub',
            editable: false,
            ticker: 'NEWCO',
            title: 'NewCo Holdings',
            exchange: 'NASDAQ Global',
            priceLow: '14.00',
            priceHigh: '16.00',
            shares: 12500000,
            ipoStatus: 'expected',
          }),
        ]),
      )
    renderPanel()
    const wed = await day('Wed, Oct 14')
    expect(within(wed).getByText('IPO')).toBeInTheDocument()
    expect(within(wed).getByText('NASDAQ Global')).toBeInTheDocument()
    expect(within(wed).getByText('$14.00–$16.00')).toBeInTheDocument()
    expect(within(wed).getByText('12,500,000')).toBeInTheDocument()
    expect(within(wed).getByText('Expected')).toBeInTheDocument()
  })

  it('labels a date-only BoJ decision as the Tokyo date, with the reason', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([
          event({
            id: '8',
            type: 'central-bank',
            source: 'seed',
            editable: false,
            title: 'BoJ policy decision',
          }),
        ]),
      )
    renderPanel()
    const wed = await day('Wed, Oct 14')
    const label = within(wed).getByText('Tokyo date')
    expect(label.getAttribute('title')).toMatch(/publishes no announcement time/)
    expect(within(wed).getByText(/publishes no announcement time/)).toHaveClass('sr-only')
    expect(within(wed).queryByText('All day')).not.toBeInTheDocument()
  })
})

describe('economic first prints', () => {
  beforeEach(() => {
    served = () => jsonResponse(200, rangeOf(ECONOMIC_ROWS))
  })

  it('shows a filled release as Actual and Prior with the unit, verbatim', async () => {
    renderPanel()
    const wed = await day('Wed, Oct 14')
    const row = rowOf(wed, 'Consumer Price Index')
    expect(within(row).getByText('0.6 % m/m').closest('[tabindex]')).toHaveTextContent(
      /^Actual 0\.6 % m\/m · Prior 0\.4 % m\/m$/,
    )
    expect(row).toHaveTextContent(/Consensus not available$/)
    // Served strings, never through a float: "0.6" is not "0.60", and
    // "-105.0" keeps the zero a Number would drop.
    expect(within(wed).getByText('0.6 % m/m')).toBeInTheDocument()
    expect(within(wed).queryByText(/0\.60/)).not.toBeInTheDocument()
    expect(within(wed).getByText('-105.0 $B')).toBeInTheDocument()
    expect(within(wed).getByText('-98.7 $B')).toBeInTheDocument()
    expect(within(wed).getByText('0.3 % m/m, NSA')).toBeInTheDocument()
  })

  /** A print is not P&L: mono figures in on-surface, never bullish/bearish —
   * a negative trade balance is not a losing position. */
  it('renders figures in on-surface mono, never bullish or bearish', async () => {
    renderPanel()
    const wed = await day('Wed, Oct 14')
    for (const text of ['0.6 % m/m', '0.4 % m/m', '-105.0 $B', '-0.1 % m/m, NSA']) {
      const figure = within(wed).getByText(text)
      expect(figure).toHaveClass('font-mono', 'tabular-nums', 'text-on-surface')
      expect(figure.className).not.toMatch(/bullish|bearish|error|outline|neutral/)
    }
  })

  it('reaches the note on keyboard focus, as the figures’ description', async () => {
    renderPanel()
    const wed = await day('Wed, Oct 14')
    const row = rowOf(wed, 'Consumer Price Index')
    const stop = within(row).getByText('0.6 % m/m').closest('[tabindex]') as HTMLElement
    expect(stop).toHaveAttribute('tabindex', '0')
    expect(stop).toHaveAccessibleDescription(CPI_NOTE)
    act(() => stop.focus())
    expect(stop).toHaveFocus()
    // Shown on hover and on focus, hidden otherwise — so it is read once,
    // as the description, not again in the reading order.
    const tip = within(row).getByRole('tooltip', { hidden: true })
    expect(tip).toHaveTextContent(CPI_NOTE)
    expect(tip).toHaveClass('hidden', 'group-hover:block', 'group-focus-within:block')
    // The ring is the global `:focus-visible` rule, gated by data-modality;
    // a local outline-none would delete it on the keyboard path.
    expect(stop.className).not.toMatch(/outline-none|ring-0/)
  })

  it('says awaiting in words, in on-surface-variant', async () => {
    renderPanel()
    const wed = await day('Wed, Oct 14')
    const row = rowOf(wed, 'Retail sales')
    expect(row).toHaveTextContent("Released, awaiting FRED's first print · Consensus not available")
    const words = within(row).getByText("Released, awaiting FRED's first print").closest('p')!
    expect(words).toHaveClass('text-on-surface-variant')
    expect(words.className).not.toMatch(/error|caution/)
    expect(row).not.toHaveTextContent(/Actual|Prior/)
  })

  it("says unavailable in the server's words, verbatim — a lapse and a refusal", async () => {
    renderPanel()
    const wed = await day('Wed, Oct 14')
    const lapse = rowOf(wed, 'Initial jobless claims')
    expect(within(lapse).getByText(LAPSE_NOTE)).toBeInTheDocument()
    expect(lapse).toHaveTextContent(`First print unavailable: ${LAPSE_NOTE}`)
    const refused = rowOf(wed, 'Existing home sales')
    const reason = within(refused).getByText(NAR_REFUSAL).closest('p')!
    expect(reason).toHaveClass('text-on-surface-variant')
    expect(reason.className).not.toMatch(/error|caution/)
    expect(refused).not.toHaveTextContent(/Actual|Prior/)
  })

  it('says upcoming at the release time on the row, with no number', async () => {
    renderPanel()
    const fri = await day('Fri, Oct 16')
    const row = rowOf(fri, 'Nonfarm payrolls')
    expect(within(row).getByText('8:30 AM')).toBeInTheDocument()
    expect(row).toHaveTextContent('Upcoming at 8:30 AM ET — no figure until it is released')
    expect(row).not.toHaveTextContent(/Actual|Prior|\bK\b/)
  })

  it('reads consensus as not available on every economic row, and no other', async () => {
    renderPanel()
    const fri = await day('Fri, Oct 16')
    expect(within(panel()).getAllByText(/Consensus not available/)).toHaveLength(7)
    expect(rowOf(fri, 'G20 summit')).not.toHaveTextContent(
      /Consensus|First print|Actual|Prior|Upcoming/,
    )
  })

  /** The owner-decision notice went with `releaseFigures`. */
  it('carries no blanket pending notice', async () => {
    renderPanel()
    await day('Wed, Oct 14')
    expect(within(panel()).queryByText(/owner decision/i)).not.toBeInTheDocument()
    expect(within(panel()).queryByText(/not filled yet|are pending/i)).not.toBeInTheDocument()
    expect(
      within(panel()).getByText(/Consensus is not available on any economic release/),
    ).toBeInTheDocument()
  })
})

describe('notices', () => {
  it('says an access_denied job is not on this plan, apart from failing', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([event()], {
          jobs: [
            job({
              job: 'calendar_ipo',
              kinds: ['ipo'],
              state: 'access_denied',
              accessDenied: true,
              message: 'IPOs: Finnhub refused this calendar on this key at Oct 12, 7:05 AM (a premium endpoint).',
            }),
          ],
        }),
      )
    renderPanel()
    await day('Wed, Oct 14')
    const line = within(panel()).getByText(/IPOs: Not on this plan/).closest('li')!
    expect(line).toHaveTextContent('Finnhub refused this calendar on this key')
    expect(line).not.toHaveClass('text-error')
  })

  it('says where coverage stops', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([event()], {
          jobs: [
            job({
              coveredThrough: '2026-11-02',
              message:
                'Earnings: last fetched Oct 12, 7:00 AM. Not covered: the job looks 21 days ahead, so dates after 2026-11-02 have not been fetched.',
            }),
          ],
        }),
      )
    renderPanel()
    await day('Wed, Oct 14')
    expect(
      within(panel()).getByText(/Dates after Mon, Nov 2 are not covered for earnings/),
    ).toBeInTheDocument()
    expect(within(panel()).getByText(/looks 21 days ahead/)).toBeInTheDocument()
  })

  it('says fresh_at_start calmly, and a dividend absence by its reason', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([event()], {
          jobs: [
            job({ state: 'fresh_at_start', message: 'Earnings: up to date as of Oct 12, 6:00 AM.' }),
            job({
              job: 'calendar_dividends',
              kinds: ['dividend'],
              state: 'skipped',
              rowsInRange: 0,
              lastSkipReason: 'announced ex-dates arrive only at the ex-date',
              message:
                'Dividends: nothing fetched at Oct 12, 7:10 AM -- announced ex-dates arrive only at the ex-date.',
            }),
          ],
        }),
      )
    renderPanel()
    await day('Wed, Oct 14')
    const fresh = within(panel()).getByText(/Earnings: Up to date/).closest('li')!
    expect(fresh).toHaveClass('text-on-surface-variant')
    expect(within(panel()).getByText(/announced ex-dates arrive only at the ex-date/)).toBeInTheDocument()
    expect(within(panel()).getByText(/Dividends: Skipped/)).toBeInTheDocument()
  })

  it('says each central-bank seed gap aloud', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([event()], {
          seedGaps: [
            { bank: 'ECB', year: 2027, kind: 'unpublished', reason: 'ECB has not published 2027 dates', coversFrom: null },
            {
              bank: 'ECB',
              year: 2026,
              kind: 'partial',
              reason: 'The calendar page lists meetings from 28/10/2026 onward only.',
              coversFrom: '2026-10-10',
            },
          ],
        }),
      )
    renderPanel()
    await day('Wed, Oct 14')
    const gaps = within(panel()).getByRole('list', { name: 'Central-bank seed gaps' })
    expect(gaps).toHaveTextContent('ECB has not published 2027 dates')
    expect(gaps).toHaveTextContent('ECB 2026 seeded only from Oct 10, 2026')
  })
})

describe('the manual-entry form', () => {
  it('adds an entry with an ET time sent as an instant with its offset', async () => {
    renderPanel()
    await within(panel()).findByText(/^Nothing scheduled/)
    const form = within(panel()).getByRole('form', { name: 'Add a calendar entry' })
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: ' G20 summit ' } })
    fireEvent.change(within(form).getByLabelText('Date'), { target: { value: '2026-10-16' } })
    fireEvent.change(within(form).getByLabelText('Time, ET (optional)'), { target: { value: '09:30' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }))

    await waitFor(() => expect(calls.some((c) => c.method === 'POST')).toBe(true))
    const post = calls.find((c) => c.method === 'POST')!
    expect(post.path).toMatch(/\/calendar\/manual$/)
    expect(post.body).toEqual({ title: 'G20 summit', date: '2026-10-16', at: '2026-10-16T09:30:00-04:00' })
    // The range is re-read after the write.
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET').length).toBe(2))
  })

  it('shows a 422 in the server’s words, in error', async () => {
    writes = () =>
      refusal(422, 'invalid_calendar_entry', "the entry's time falls on 2026-10-17 in New York, not 2026-10-16")
    renderPanel()
    await within(panel()).findByText(/^Nothing scheduled/)
    const form = within(panel()).getByRole('form', { name: 'Add a calendar entry' })
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'G20 summit' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Add entry' }))

    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent("Not saved: the entry's time falls on 2026-10-17 in New York")
    expect(alert).toHaveClass('text-error')
    expect(alert).not.toHaveClass('text-bearish')
  })

  it('edits a manual row in place', async () => {
    served = () => jsonResponse(200, rangeOf([event({ id: '9', at: '2026-10-14T13:30:00Z' })]))
    writes = () => jsonResponse(200, event({ id: '9', title: 'G20 leaders summit' }))
    renderPanel()
    const wed = await day('Wed, Oct 14')
    fireEvent.click(within(wed).getByRole('button', { name: 'Edit G20 summit' }))

    const form = within(wed).getByRole('form', { name: 'Edit G20 summit' })
    expect(within(form).getByLabelText('Time, ET (optional)')).toHaveValue('09:30')
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'G20 leaders summit' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const put = calls.find((c) => c.method === 'PUT')!
    expect(put.path).toMatch(/\/calendar\/manual\/9$/)
    expect(put.body).toEqual({
      title: 'G20 leaders summit',
      date: '2026-10-14',
      at: '2026-10-14T09:30:00-04:00',
    })
  })

  it('renders a 409 as a row it cannot edit, naming its source', async () => {
    served = () => jsonResponse(200, rangeOf([event({ id: '9', source: 'manual' })]))
    writes = () =>
      refusal(409, 'calendar_event_not_editable', 'calendar event 9 is a seed central-bank row')
    renderPanel()
    const wed = await day('Wed, Oct 14')
    fireEvent.click(within(wed).getByRole('button', { name: 'Edit G20 summit' }))
    const form = within(wed).getByRole('form', { name: 'Edit G20 summit' })
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }))

    const alert = await within(form).findByRole('alert')
    expect(alert).toHaveTextContent("This row comes from a manual entry and can't be edited")
    expect(alert).toHaveClass('text-error')
  })

  it('offers no edit or remove on a vendor or seed row', async () => {
    served = () =>
      jsonResponse(
        200,
        rangeOf([event({ id: '8', type: 'central-bank', source: 'seed', editable: false, title: 'BoJ policy decision' })]),
      )
    renderPanel()
    const wed = await day('Wed, Oct 14')
    expect(within(wed).queryByRole('button', { name: /Edit|Remove/ })).not.toBeInTheDocument()
  })

  it('removes only after a confirm that names the row and its day', async () => {
    served = () => jsonResponse(200, rangeOf([event({ id: '9', date: '2026-10-16' })]))
    writes = () => jsonResponse(204, null)
    renderPanel()
    const fri = await day('Fri, Oct 16')
    fireEvent.click(within(fri).getByRole('button', { name: 'Remove G20 summit' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveTextContent("Remove 'G20 summit' on Fri, Oct 16 from the calendar?")
    expect(dialog).not.toHaveTextContent(/are you sure/i)
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove entry' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE')).toBe(true))
    expect(calls.find((c) => c.method === 'DELETE')!.path).toMatch(/\/calendar\/manual\/9$/)
    await waitFor(() => expect(calls.filter((c) => c.method === 'GET').length).toBe(2))
    expect(within(panel()).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('sends nothing when the remove confirm is cancelled', async () => {
    served = () => jsonResponse(200, rangeOf([event({ id: '9' })]))
    renderPanel()
    const wed = await day('Wed, Oct 14')
    fireEvent.click(within(wed).getByRole('button', { name: 'Remove G20 summit' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
  })
})
