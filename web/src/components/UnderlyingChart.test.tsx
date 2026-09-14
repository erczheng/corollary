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

/** One session: five-minute bars from 09:30 ET on Fri 2026-09-11 — what
 * `1D` asks for. **A full regular-hours session is 78 of them**, ending
 * 15:55 ET, now that the server filters extended hours out.
 *
 * It steps five minutes from one day's open whatever `count` is, so it
 * cannot stand in for a multi-session window: 192 of these ran to 01:25 ET
 * on the 12th, which put a fixture named "the day" onto the concatenated
 * multi-session path with a separator under the `1D` button — passing only
 * because nothing is drawn in this file. A week is `weekBars`. */
function bars(count = 78): IntradayPoint[] {
  const open = Date.UTC(2026, 8, 11, 13, 30)
  return Array.from({ length: count }, (_, i) => ({
    at: new Date(open + i * 5 * 60_000).toISOString(),
    value: 180 + i * 0.5,
  }))
}

/** A week: fifteen-minute bars over five ET sessions — what `1W` asks for,
 * and ~130 points, since a regular-hours session is 26 of them.
 *
 * The days are Tue 8 → Fri 11 then Mon 14, which is what a rolling five
 * sessions looks like here (Mon the 7th is Labor Day). A 1W fixture has to
 * actually span days: the concatenation, the seams and the dated ticks all
 * key off the ET day changing. */
function weekBars(perSession = 26): IntradayPoint[] {
  return [8, 9, 10, 11, 14].flatMap((day, s) =>
    Array.from({ length: perSession }, (_, i) => ({
      at: new Date(Date.UTC(2026, 8, day, 13, 30) + i * 15 * 60_000).toISOString(),
      value: 180 + (s * perSession + i) * 0.1,
    })),
  )
}

/** Calendar dates walked back from Fri 2026-09-11, not `2026-09-${i}`:
 * past the 30th that is not a date, and `formatSessionDay` throws
 * `RangeError: Invalid time value` on it *during render* — the same shape
 * of crash this file exists to pin, arriving from the fixture instead of
 * from the server. It went unseen while the awaits below resolved against
 * the pending line and the tests ended before that paint. */
function closes(count: number): PricePoint[] {
  const end = Date.UTC(2026, 8, 11)
  return Array.from({ length: count }, (_, i) => ({
    date: new Date(end - (count - 1 - i) * 86_400_000).toISOString().slice(0, 10),
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

/** Settled, not merely rendered.
 *
 * Both lines that say a request is in flight carry `role="status"`: the
 * pending one ("Reading NVDA over 3M at 1D bars...") and the stale one
 * ("Reading 1D..."). Both also match the substrings these tests wait on, so
 * `findByText(/over 3M/)` resolves against the line that says there is no
 * answer yet and the next `fireEvent.click` fires mid-flight. Waiting for
 * the status line to *leave* is the barrier those awaits were reaching for,
 * and it cannot collide with the readout it is waiting for. */
async function settled() {
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
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
    // The whole point: a session of 78 five-minute bars where there used
    // to be one daily close.
    const windows = stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1D' ? [quote({ intraday: bars(78) })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1D' }))

    await waitFor(() => expect(windows).toContain('1D/5Min'))
    await settled()
    // A series with something in it: the readout measures the window on
    // screen, and it is withheld entirely below two points.
    expect(screen.getByText(/over 1D/)).toBeInTheDocument()
    expect(screen.queryByText(/no line to draw yet/)).not.toBeInTheDocument()
  })

  it('asks for the week finer than hourly', async () => {
    const windows = stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W' ? [quote({ intraday: weekBars() })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))

    await waitFor(() => expect(windows).toContain('1W/15Min'))
  })

  /** The axis for a multi-session intraday window is ordinal — Friday 16:00
   * butts against Monday 09:30 — and the two readouts beside it still
   * measure the window on screen rather than the day. Concatenating changed
   * where points sit, not what is being measured. The separators the
   * concatenation needs are counted on a drawn SVG in
   * `UnderlyingChart.sessions.test.tsx`; nothing is drawn here. */
  it('still measures the window on screen across concatenated sessions', async () => {
    // 56 bars over three ET days at the 15Min the `1W` button asks for:
    // two full regular-hours sessions (Fri, Mon — 26 bars each, 09:30 to
    // 15:45) and the first four bars of a third, with two overnight gaps
    // between them that the ordinal axis closes. Values run by position,
    // 180 to 185.5 — +$5.50 on a 180 start, +3.06%.
    const day = (d: number, count: number, from: number) =>
      Array.from({ length: count }, (_, i) => ({
        at: new Date(Date.UTC(2026, 8, d, 13, 30) + i * 15 * 60_000).toISOString(),
        value: 180 + (from + i) * 0.1,
      }))
    const intraday = [...day(11, 26, 0), ...day(14, 26, 26), ...day(15, 4, 52)]
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W' ? [quote({ intraday })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))
    await settled()

    expect(screen.getByText(/over 1W/)).toBeInTheDocument()
    expect(screen.getByText(/3\.06%/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
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
      await settled()

      fireEvent.click(screen.getByRole('button', { name: '1D' }))

      await settled()
      expect(screen.getByText(/over 1D/)).toBeInTheDocument()
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

  /** The blank page. A server that predates the intraday split answers
   * `/api/markets/underlyings` with `history` and **no `intraday` key at
   * all** — the field is typed `IntradayPoint[]` on this side, so
   * `quoteSeries` read `.length` off `undefined` and threw during render.
   * React unmounted the whole Markets tree, not just this chart.
   *
   * Every fixture above fills both fields, which is exactly why a green
   * suite could not see it. These two pass the payload the running server
   * actually serves. */
  describe('a response that does not carry both series fields', () => {
    it('draws the series it did get rather than throwing', async () => {
      // `history` populated, `intraday` absent — the live skew today.
      stubUnderlyings(() =>
        jsonResponse(200, [
          {
            symbol: 'NVDA',
            price: 184.2,
            previousClose: 181,
            change: 3.2,
            changePct: 1.77,
            history: closes(20),
          },
        ]),
      )
      renderChart()

      // The price renders only once the quote has arrived, so it cannot
      // match the pending line the way /over 3M/ alone can — that line
      // reads "Reading NVDA over 3M at 1D bars…" and is on screen
      // before the payload that used to throw is ever touched.
      expect(await screen.findByText(/\$184\.20/)).toBeInTheDocument()
      // Drawn, not blanked: twenty closes did arrive and they are still
      // the best thing to show. What is gone is the caption over them.
      expect(screen.queryByText(/carried no price series/)).toBeNull()
      expect(screen.queryByText(/No session for NVDA in this window/)).toBeNull()
      expect(screen.getByRole('button', { name: '1Y' })).toBeInTheDocument()
    })

    /** The hole this describe block used to leave open. `3M` asks for
     * `1D` bars and a stale server answers `1D` bars, so the
     * requested-against-served check matches and says nothing — while
     * the payload is the server's own default window, not the three
     * months that were asked for. The figure printed `over 3M` across
     * whatever that default is, and `Last market day` named the newest
     * of it, with no alert anywhere on screen. Identical on `1M`, `YTD`,
     * `1Y` and `All`: five of the seven buttons.
     *
     * The missing `intraday` key is what makes it detectable without
     * inferring anything — a build that does not carry a field this page
     * reads cannot be assumed to have honoured `period` either. */
    it('withholds the window figure on a daily range, where the resolution matches', async () => {
      stubUnderlyings(() =>
        jsonResponse(200, [
          {
            symbol: 'NVDA',
            price: 184.2,
            previousClose: 181,
            change: 3.2,
            changePct: 1.77,
            history: closes(20),
          },
        ]),
      )
      renderChart()
      await settled()

      // Twenty closes from 170 to 189 is +$19.00 / +11.18%, and on the
      // old code it printed under "over 3M". Matching the percent rather
      // than /over 3M/: the alert would carry that substring itself.
      expect(screen.queryAllByText(/11\.18%/)).toHaveLength(0)
      expect(screen.queryByText(/Last market day/)).toBeNull()
      expect(await screen.findByRole('alert')).toHaveTextContent(
        /left out one of the two price series fields/,
      )
    })

    it('keeps saying so on the other daily ranges, not just the one it opened on', async () => {
      stubUnderlyings(() =>
        jsonResponse(200, [
          {
            symbol: 'NVDA',
            price: 184.2,
            previousClose: 181,
            change: 3.2,
            changePct: 1.77,
            history: closes(20),
          },
        ]),
      )
      renderChart()
      await settled()

      fireEvent.click(screen.getByRole('button', { name: '1Y' }))
      await settled()

      expect(screen.queryAllByText(/11\.18%/)).toHaveLength(0)
      expect(await screen.findByRole('alert')).toHaveTextContent(/honoured the 1Y window/)
    })

    it('says the response carried no series rather than calling it a quiet Sunday', async () => {
      // Neither field present. "Both empty" is the server stating that the
      // window held no session; "neither present" is a response this client
      // cannot read a series out of, and the two must not share wording.
      stubUnderlyings(() =>
        jsonResponse(200, [
          { symbol: 'NVDA', price: 184.2, previousClose: 181, change: 3.2, changePct: 1.77 },
        ]),
      )
      renderChart()

      expect(await screen.findByRole('alert')).toHaveTextContent(/carried no price series/)
      expect(screen.queryByText(/No session for NVDA in this window/)).not.toBeInTheDocument()
    })
  })

  /** The same stale server, one crash later. It declares `history_days`,
   * ignores `period` and `timeframe`, and answers every range with the
   * same daily closes. Before the crash fix that payload threw and the
   * page blanked — unmistakable. After it the page renders, and that is
   * the problem: `+$19.00 +11.18% over 1D` is a money figure under a
   * window label it does not measure, beside a dated x-axis under a
   * button that says intraday, with nothing on screen qualifying it.
   *
   * The served resolution stays authoritative for every label — this asks
   * only whether the answer is an answer to the question that was asked.
   * `/api/markets/underlyings` echoes no `timeframe` the way
   * `/api/account/history` does, so requested-against-served is the only
   * form that question can take here. */
  describe('a server answering at a resolution nobody asked for', () => {
    /** Every range served the same daily series, whatever the query said.
     * That is what "ignores `period`" looks like from the browser. */
    function staleServer() {
      return stubUnderlyings(() => jsonResponse(200, [quote({ history: closes(20) })]))
    }

    it('withholds the window figure when the day comes back as daily closes', async () => {
      staleServer()
      renderChart()
      await settled()
      // 3M asks for daily bars, so this payload *is* an answer to that
      // question and the figure is printed. The check has to stay quiet
      // here or it is not a check.
      expect(screen.getByText(/over 3M/)).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: '1D' }))
      await settled()

      // Not `/over 1D/`: the line that explains the withholding says "over
      // 1D" itself, so matching on that would pass for the wrong reason —
      // the same substring collision the barriers above exist for. What
      // has to be gone is the money figure. `queryAllByText` because a
      // count of zero is the assertion, and the singular form throws on a
      // second match instead of reporting one.
      expect(screen.queryAllByText(/11.18%/)).toHaveLength(0)
      expect(await screen.findByRole('alert')).toHaveTextContent(/answered with daily closes/)
    })

    it('withholds the session lead too, and keeps the chart and the ranges', async () => {
      staleServer()
      renderChart()
      await settled()

      fireEvent.click(screen.getByRole('button', { name: '1D' }))
      await settled()

      // As unanchored as the change figure: it names the last of twenty
      // closes under a control reading `1D`.
      expect(screen.queryByText(/Last market day/)).toBeNull()
      // Not an empty state and not a blanked page. The series that did
      // arrive is still drawn, and every range is still one click away.
      expect(screen.queryByText(/No session for NVDA in this window/)).toBeNull()
      expect(screen.queryByText(/carried no price series/)).toBeNull()
      expect(screen.getByRole('button', { name: '3M' })).toBeInTheDocument()
    })
  })

  it('prints the figure when the answer matches the question', async () => {
    // The same control against the current server: 1D asked at 5Min,
    // answered intraday. A check that fires here fires always.
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1D' ? [quote({ intraday: bars(78) })] : [quote({ history: closes(20) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1D' }))
    await settled()

    expect(screen.getByText(/over 1D/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('names the window it is reading while the first one is in flight', async () => {
    stubUnderlyings(() => new Promise<never>(() => {}) as unknown as Response)
    renderChart()

    expect(await screen.findByRole('status')).toHaveTextContent('Reading NVDA over 3M at 1D bars…')
  })
})
