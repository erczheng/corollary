import { describe, it, expect, afterEach, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { UnderlyingChart } from './UnderlyingChart'
import type { IntradayPoint, PricePoint, UnderlyingQuote } from '../lib/types'

/** The defect this file pins: the range control used to slice one fixed
 * daily series, so `1D` rendered a single point and `1W` about five. It asks
 * for the resolution it displays now, and which resolution came back is read
 * off the response rather than assumed from the request.
 *
 * `ResponsiveContainer` measures zero in jsdom, so Recharts draws no SVG
 * here and the assertions are on what the component *says*: which window it
 * asked the server for, and whether it has a series to draw or one of the
 * stated reasons it does not. */

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function quote(parts: Partial<UnderlyingQuote> = {}): UnderlyingQuote {
  return {
    symbol: 'NVDA',
    price: 184.2,
    previousClose: 181.0,
    change: 3.2,
    changePct: 1.77,
    history: [],
    intraday: [],
    ...parts,
  }
}

/** 09:30 ET onward, five minutes apart — what `1D` now asks for. */
function bars(count: number): IntradayPoint[] {
  const open = Date.UTC(2026, 8, 11, 13, 30)
  return Array.from({ length: count }, (_, i) => ({
    at: new Date(open + i * 5 * 60_000).toISOString(),
    value: 180 + i * 0.5,
  }))
}

function closes(count: number): PricePoint[] {
  return Array.from({ length: count }, (_, i) => ({
    date: `2026-09-${String(i + 1).padStart(2, '0')}`,
    value: 170 + i,
  }))
}

/** Answers every underlyings request, recording the window each one asked
 * for. `byWindow` lets one test answer 1D differently from 3M. */
function stubUnderlyings(byWindow: (period: string, timeframe: string) => Response) {
  const windows: string[] = []
  const fetchMock = vi.fn((input: unknown) => {
    const url = String(input)
    const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
    const period = params.get('period') ?? ''
    const timeframe = params.get('timeframe') ?? ''
    windows.push(`${period}/${timeframe}`)
    return Promise.resolve(byWindow(period, timeframe))
  })
  vi.stubGlobal('fetch', fetchMock)
  return windows
}

function renderChart() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  render(<UnderlyingChart symbol="NVDA" />, { wrapper: Wrapper })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('UnderlyingChart', () => {
  it('opens on three months of daily closes', async () => {
    const windows = stubUnderlyings(() => jsonResponse(200, [quote({ history: closes(20) })]))
    renderChart()

    await waitFor(() => expect(windows).toContain('3M/1D'))
  })

  it('asks for the day at five-minute bars and draws all of them', async () => {
    // The whole point: 192 bars where there used to be one daily close.
    const windows = stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1D' ? [quote({ intraday: bars(192) })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await screen.findByText(/over 3M/)

    fireEvent.click(screen.getByRole('button', { name: '1D' }))

    await waitFor(() => expect(windows).toContain('1D/5Min'))
    // A series with something in it: the readout measures the window on
    // screen, and it is withheld entirely below two points.
    expect(await screen.findByText(/over 1D/)).toBeInTheDocument()
    expect(screen.queryByText(/no line to draw yet/)).not.toBeInTheDocument()
  })

  it('asks for the week finer than hourly', async () => {
    const windows = stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W' ? [quote({ intraday: bars(256) })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await screen.findByText(/over 3M/)

    fireEvent.click(screen.getByRole('button', { name: '1W' }))

    await waitFor(() => expect(windows).toContain('1W/15Min'))
  })

  it('says there was no session rather than drawing an empty chart', async () => {
    // Both series fields empty — `1D` asked on a Sunday. Neither a flat line
    // nor a blank plot: the window held no session and says so.
    stubUnderlyings(() => jsonResponse(200, [quote()]))
    renderChart()

    expect(await screen.findByText(/No session for NVDA in this window/)).toBeInTheDocument()
  })

  it('renders the server’s own words when a window is refused, and keeps the ranges', async () => {
    stubUnderlyings(() =>
      jsonResponse(422, {
        error: {
          code: 'invalid_series_window',
          message: 'Ask for 1W at 5Min instead, or shorten the period.',
        },
      }),
    )
    renderChart()

    expect(await screen.findByRole('alert')).toHaveTextContent('Ask for 1W at 5Min instead')
    expect(screen.getByText(/more points than the server will return at once/)).toBeInTheDocument()
    // Still a way back to a range that works.
    expect(screen.getByRole('button', { name: '3M' })).toBeInTheDocument()
  })

  /** `1D` on a Saturday draws Friday. That is the right data — there is no
   * session today — but the control says `1D`, so the chart has to name the
   * day it is actually showing. The test is the newest point against
   * today's ET date, the same one the Markets volume column makes. */
  describe('which session is on screen', () => {
    it('names the day a daily series ends on', async () => {
      // Eleven closes ending 2026-09-11, a Friday.
      stubUnderlyings(() => jsonResponse(200, [quote({ history: closes(11) })]))
      renderChart()

      expect(await screen.findByText(/Last market day/)).toHaveTextContent('Fri, Sep 11')
    })

    it('names it from the ET day an intraday bar fell on', async () => {
      // 78 five-minute bars is one 09:30–16:00 session, ending 19:55Z —
      // 3:55 PM ET on the 11th.
      stubUnderlyings((period) =>
        jsonResponse(
          200,
          period === '1D' ? [quote({ intraday: bars(78) })] : [quote({ history: closes(11) })],
        ),
      )
      renderChart()
      await screen.findByText(/over 3M/)

      fireEvent.click(screen.getByRole('button', { name: '1D' }))

      expect(await screen.findByText(/over 1D/)).toBeInTheDocument()
      expect(screen.getByText(/Last market day/)).toHaveTextContent('Fri, Sep 11')
    })

    it('leaves the no-session state its own wording', async () => {
      // Both series fields empty — `1D` asked on a Sunday. A day label on
      // top of that would name a session that is not drawn.
      stubUnderlyings(() => jsonResponse(200, [quote()]))
      renderChart()

      expect(await screen.findByText(/No session for NVDA in this window/)).toBeInTheDocument()
      expect(screen.queryByText(/Last market day/)).not.toBeInTheDocument()
    })
  })

  it('names the window it is reading while the first one is in flight', async () => {
    stubUnderlyings(() => new Promise<never>(() => {}) as unknown as Response)
    renderChart()

    expect(await screen.findByRole('status')).toHaveTextContent('Reading NVDA over 3M at 1D bars…')
  })
})
