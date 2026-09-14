import { describe, it, expect, afterEach, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { cloneElement, isValidElement, type ReactElement, type ReactNode } from 'react'
import { UnderlyingChart } from './UnderlyingChart'
import type { IntradayPoint, PricePoint, UnderlyingQuote } from '../lib/types'

/** The concatenated intraday axis, asserted on the **drawn SVG**.
 *
 * `UnderlyingChart.test.tsx` pins everything this component *says*, and does
 * so with no chart on screen at all: `ResponsiveContainer` measures 0×0 in
 * jsdom, Recharts draws nothing at zero size, and so a render test there
 * never runs a tick formatter (the trap commit `ff4e766` documents). The
 * ordinal axis adds claims that are about the drawing rather than the
 * wording — **one session separator per boundary, and none inside a single
 * session**, and a **crosshair that resolves an index back to its own
 * bar** — so this file gives the container a size and exercises them.
 *
 * What the numeric axis actually changed is worth stating here, because the
 * name "concatenated" oversells it: the category axis it replaced was a
 * point scale and the sessions were *already* butted together, with no
 * overnight gap drawn at any range. The new axis buys the **seams and the
 * tick positions** — a `ReferenceLine` on a category axis can only sit on
 * top of a bar, never in the half-step between two, and a category axis
 * takes no explicit `ticks` array. Which is why those two are what this
 * file counts.
 *
 * Kept in its own file because the mock is module-scoped: every other
 * assertion in this suite is better off with the real, unmeasured container
 * it was written against. The arithmetic behind the seams
 * (`api.ts#sessionBoundaries`, `ordinalSeries`, `ordinalTicks`,
 * `seriesPointLabel`) is tested as plain functions in `api.test.ts`, which
 * is where the fail-closed and ET-day rules live. This only asks whether the
 * component draws what those functions return. */
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    // A fixed box, handed straight to the chart as props. Recharts accepts
    // `width`/`height` on the chart itself, which is the only way to get an
    // SVG out of it in jsdom.
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as ReactElement<{ width: number; height: number }>, {
            width: 800,
            height: 240,
          })
        : null,
  }
})

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

/** `previousClose: null` deliberately: the prev-close line is a
 * `ReferenceLine` too, and the count below is of separators. With a close on
 * it the assertion would be "boundaries + 1", which is the kind of figure
 * that stays passing through an off-by-one. */
function quote(parts: Partial<UnderlyingQuote> = {}): UnderlyingQuote {
  return {
    symbol: 'NVDA',
    price: 184.2,
    previousClose: null,
    change: 3.2,
    changePct: 1.77,
    history: [],
    intraday: [],
    ...parts,
  }
}

/** One regular-hours session: `count` five-minute bars from 09:30 ET on
 * 2026-09-`day`. 78 is a full one now that the server filters extended
 * hours out, so a week of them is five ET days with a seam between each
 * pair — which is the thing being counted below.
 *
 * The step stays five minutes even in the `1W` fixtures, where the real
 * server answers 15Min: nothing in this file reads the interval, only the
 * ET day each bar falls on. Keep it that way — a fixture whose `count`
 * silently runs past 16:00 into the next ET day is how a single-session
 * test ends up on the multi-session path. */
function session(day: number, count = 78): IntradayPoint[] {
  const open = Date.UTC(2026, 8, day, 13, 30)
  return Array.from({ length: count }, (_, i) => ({
    at: new Date(open + i * 5 * 60_000).toISOString(),
    value: 180 + i * 0.1,
  }))
}

function closes(count: number): PricePoint[] {
  const end = Date.UTC(2026, 8, 11)
  return Array.from({ length: count }, (_, i) => ({
    date: new Date(end - (count - 1 - i) * 86_400_000).toISOString().slice(0, 10),
    value: 170 + i,
  }))
}

function stubUnderlyings(byWindow: (period: string, timeframe: string) => Response) {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: unknown) => {
      const url = String(input)
      const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
      return Promise.resolve(byWindow(params.get('period') ?? '', params.get('timeframe') ?? ''))
    }),
  )
}

function renderChart() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<UnderlyingChart symbol="NVDA" />, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  })
}

/** Settled, not merely rendered — the same barrier the sibling file uses,
 * and for the same reason: the pending line and the stale line both carry
 * `role="status"` and both match the substrings these assertions wait on. */
async function settled() {
  await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
}

function separators(): NodeListOf<Element> {
  return document.querySelectorAll('.recharts-reference-line-line')
}

/** The plot area of the 800×240 box the mock hands the chart: the y-axis is
 * 72 wide and the right margin is 8, so the line runs from x=72 to x=792.
 * A point scale and a numeric domain both put point `i` of `n` at
 * `72 + (i / (n - 1)) * 720`, which is what makes a hover addressable by
 * index. Fixtures here are kept to a handful of points so one bar is ~144px
 * wide and a hover cannot land on its neighbour. */
function xOfPoint(index: number, count: number): number {
  return 72 + (index / (count - 1)) * 720
}

/** Recharts' own hover path — `mouseMove` on the wrapper it listens on, not
 * a formatter called directly. The point is to exercise the wiring between
 * the axis and `labelFormatter`, which is where an index can be handed to a
 * formatter expecting a key. */
function hover(index: number, count: number) {
  const wrapper = document.querySelector('.recharts-wrapper')
  if (wrapper === null) throw new Error('no chart drawn to hover over')
  fireEvent.mouseOver(wrapper, { clientX: xOfPoint(index, count), clientY: 120 })
  fireEvent.mouseMove(wrapper, { clientX: xOfPoint(index, count), clientY: 120 })
}

/** Scoped to the tooltip, deliberately. The axis ticks render dates too, so
 * a document-wide text query would pass on a tick while the crosshair read
 * "0". */
function tooltipText(): string {
  return document.querySelector('.recharts-tooltip-wrapper')?.textContent ?? ''
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('UnderlyingChart session separators', () => {
  it('draws one hairline per session boundary, not one per point', async () => {
    // Three sessions: Thu, Fri, Mon. Two seams — 234 bars, and a separator
    // per point would be 234 lines.
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W'
          ? [quote({ intraday: [...session(10), ...session(11), ...session(14)] })]
          : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))
    await settled()

    await waitFor(() => expect(separators()).toHaveLength(2))
  })

  it('draws none at all inside a single session', async () => {
    // 1D: 78 bars, one day. A rule here would assert a break that did not
    // happen — sessions butting together with nothing between them read as
    // one continuous session, and so does one session cut in half.
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1D' ? [quote({ intraday: session(11) })] : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1D' }))
    await settled()

    // Drawn — the chart is there, with an axis and a line.
    await waitFor(() => expect(document.querySelector('.recharts-line')).not.toBeNull())
    expect(separators()).toHaveLength(0)
  })

  it('leaves the daily path on its dated axis, with no seams', async () => {
    // Sixty closes, one per session: there is a session boundary at every
    // point, so a rule at each of them would be sixty rules and says
    // nothing — which is why the daily path keeps the category axis rather
    // than because its data is "evenly spaced". Even spacing is what a
    // category axis does to whatever it is handed; hand this path weekly
    // bars and it will space those evenly too. The axis reading dates is
    // what says the path was not touched.
    stubUnderlyings(() => jsonResponse(200, [quote({ history: closes(60) })]))
    renderChart()
    await settled()

    await waitFor(() => expect(document.querySelector('.recharts-line')).not.toBeNull())
    expect(separators()).toHaveLength(0)
    // A daily tick is a calendar date rendered in UTC. Its presence also
    // proves the tick formatter ran, which is the thing a zero-sized
    // container silently skips.
    expect(screen.getByText(/Sep 11, 2026/)).toBeInTheDocument()
  })

  it('labels the axis by day across a concatenated week', async () => {
    // Two sessions, so the ticks are the session opens rather than clock
    // times: 9:30 AM appearing twice with no date would not say which day
    // either belonged to.
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W'
          ? [quote({ intraday: [...session(11), ...session(14)] })]
          : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))
    await settled()

    await waitFor(() => expect(separators()).toHaveLength(1))
    expect(screen.getByText(/Sep 11, 9:30 AM/)).toBeInTheDocument()
    expect(screen.getByText(/Sep 14, 9:30 AM/)).toBeInTheDocument()
  })
})

/** The crosshair's label, which is the **whole compensation** for an axis
 * that no longer encodes time. `seriesPointLabel` and
 * `formatSessionDateTimeET` are tested as pure functions elsewhere; what is
 * only testable here is the step between them and a hovered bar.
 *
 * On the ordinal axis Recharts hands `labelFormatter` the **index**, so the
 * component resolves it back through `points[index].key` before formatting.
 * Invert that ternary, or drop the lookup, and the chart still renders, the
 * separators still count, and the tooltip reads `0` — on a concatenated week
 * that misattributes Monday's spike to Friday. */
describe('UnderlyingChart tooltip', () => {
  it('resolves an ordinal index back to its own bar, with the day and the time', async () => {
    // Three bars a day, two days: coarse on purpose, so a hover is
    // unambiguous about which bar it asked for.
    stubUnderlyings((period) =>
      jsonResponse(
        200,
        period === '1W'
          ? [quote({ intraday: [...session(11, 3), ...session(14, 3)] })]
          : [quote({ history: closes(60) })],
      ),
    )
    renderChart()
    await settled()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))
    await settled()
    await waitFor(() => expect(separators()).toHaveLength(1))

    // Index 3 is Monday's open — the first bar past the seam, and the one
    // an index-as-key bug attributes to the Friday.
    hover(3, 6)

    await waitFor(() => expect(tooltipText()).toContain('Mon, Sep 14, 2026'))
    expect(tooltipText()).toContain('9:30 AM')
    expect(tooltipText()).toContain('ET')
    // Not the index, and not the *other* session — the two things a
    // broken resolution produces.
    expect(tooltipText()).not.toContain('Sep 11')

    // The bar before the seam is Friday's last, and reads as Friday.
    hover(2, 6)
    await waitFor(() => expect(tooltipText()).toContain('Fri, Sep 11, 2026'))
    expect(tooltipText()).toContain('9:40 AM')
  })

  it('keeps resolving the daily path from its own key', async () => {
    // The other branch of the same ternary. Here Recharts hands over the
    // category value, which already *is* the key, and a lookup by index
    // would read the wrong date or none at all.
    stubUnderlyings(() => jsonResponse(200, [quote({ history: closes(6) })]))
    renderChart()
    await settled()
    await waitFor(() => expect(document.querySelector('.recharts-line')).not.toBeNull())

    // `closes(6)` walks back from Fri 2026-09-11, so index 3 is the 9th.
    hover(3, 6)

    await waitFor(() => expect(tooltipText()).toContain('Sep 9, 2026'))
    // A daily key is a calendar date and formats in UTC: rendered in ET it
    // would name the 8th.
    expect(tooltipText()).not.toContain('Sep 8')
  })
})
