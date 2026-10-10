import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { SECTOR_CONSENSUS, SENTIMENT_COMPONENTS, SOCIAL_ATTENTION } from '../lib/mockData'
import { compositeScore } from '../lib/news'
import type { NewsFeed, NewsItem, WatchList } from '../lib/types'

/** The feed and the watch list are the engine's since Phase 3 step 4, so
 * every test here answers `GET /api/news` and `/api/news/watch` from a
 * stub. The shapes are `corollary/api/schemas.py`'s `NewsFeed` and
 * `WatchList`, camelCased as the API serves them. In step 4 every item is
 * `unclassified` with no tier and no source — the stub says so too, since a
 * labelled fixture would test a state the server cannot produce yet. */

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function refusal(status: number, code: string, message: string): Response {
  return jsonResponse(status, { error: { code, message } })
}

function item(overrides: Partial<NewsItem> = {}): NewsItem {
  return {
    id: '101:AAPL',
    time: '2026-09-25T14:32:00Z',
    ticker: 'AAPL',
    headline: 'Apple widens its buyback by $20B',
    sentiment: 'unclassified',
    publisher: 'Reuters',
    sector: 'Other',
    tier: null,
    url: 'https://www.reuters.com/apple-buyback',
    source: null,
    demoted: false,
    ...overrides,
  }
}

const ITEMS: NewsItem[] = [
  item(),
  item({
    id: '102:MARKET',
    time: '2026-09-25T13:05:00Z',
    ticker: 'MARKET',
    headline: 'Treasury yields climb ahead of the payrolls print',
    publisher: null,
    sector: 'Macro',
    url: 'https://example.com/yields',
  }),
]

function feed(overrides: Partial<NewsFeed> = {}): NewsFeed {
  const items = overrides.items ?? ITEMS
  return {
    items,
    total: items.length,
    limit: 25,
    offset: 0,
    hasMore: false,
    lookback: 'all',
    scope: 'watch',
    sort: 'newest',
    since: null,
    sectorsAvailable: true,
    seedAsOf: '2026-09-01',
    ...overrides,
  }
}

function watchList(overrides: Partial<WatchList> = {}): WatchList {
  return {
    manual: [
      { ticker: 'PLTR', addedAt: '2026-09-24T15:00:00Z' },
      { ticker: 'SOFI', addedAt: '2026-09-25T13:10:00Z' },
    ],
    symbols: 61,
    manualCount: 2,
    cap: 34,
    remaining: 32,
    positionUnderlyings: 3,
    positionsAsOf: '2026-09-25T14:00:00Z',
    seedMissing: false,
    assetListAvailable: true,
    assetListFetchedAt: '2026-09-25T12:00:00Z',
    ...overrides,
  }
}

interface Stub {
  /** Answers `GET /api/news`, given the request's query. */
  news?: (params: URLSearchParams) => Response | Promise<Response>
  post?: Response
}

let served: WatchList
let calls: { method: string; path: string; params: URLSearchParams }[]

function stubFetch(stub: Stub = {}) {
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), 'http://127.0.0.1')
    const method = init?.method ?? 'GET'
    const path = url.pathname
    calls.push({ method, path, params: url.searchParams })

    if (path.endsWith('/news')) {
      return Promise.resolve(stub.news ? stub.news(url.searchParams) : jsonResponse(200, feed()))
    }
    const watch = path.match(/\/news\/watch(?:\/([^/]+))?$/)
    if (watch) {
      const ticker = watch[1] ? decodeURIComponent(watch[1]) : null
      if (method === 'DELETE' && ticker) {
        served = { ...served, manual: served.manual.filter((m) => m.ticker !== ticker) }
        return Promise.resolve(jsonResponse(200, served))
      }
      if (method === 'POST' && ticker) {
        if (stub.post) return Promise.resolve(stub.post)
        served = {
          ...served,
          manual: [...served.manual, { ticker, addedAt: '2026-09-25T15:00:00Z' }],
        }
        return Promise.resolve(jsonResponse(200, served))
      }
      return Promise.resolve(jsonResponse(200, served))
    }
    // Nothing else on this page reads the engine. A 200 with an empty body
    // keeps an unexpected call from reading as the failure under test.
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function newsRequests(): URLSearchParams[] {
  return calls.filter((c) => c.method === 'GET' && c.path.endsWith('/news')).map((c) => c.params)
}

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location. Same guard the other page tests
  // carry: without it a test that runs after a navigation starts on the
  // wrong page.
  window.history.pushState({}, '', '/news')
  useUIStore.setState({ ...initialState }, true)
  queryClient.clear()
  calls = []
  served = watchList()
  stubFetch()
})

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

function feedSection(): HTMLElement {
  return section('Latest intel')
}

/** The feed's table, once it has rendered — found by its header, because
 * the loading skeleton is a table too. */
async function feedTable(): Promise<HTMLElement> {
  const header = await within(feedSection()).findByRole('columnheader', { name: 'Headline' })
  return header.closest('table')!
}

function bodyRows(table: HTMLElement): HTMLElement[] {
  return within(table).getAllByRole('row').slice(1)
}

function column(table: HTMLElement, index: number): string[] {
  return bodyRows(table).map((r) => within(r).getAllByRole('cell')[index].textContent ?? '')
}

describe('the News page', () => {
  it('renders every section, the watch list included', async () => {
    render(<App />)

    for (const name of [
      'Latest intel',
      'Market Sentiment',
      'Watch list',
      'Social Attention',
      'Top rated by sector',
      'Market calendar',
    ]) {
      expect(section(name)).toBeInTheDocument()
    }
    await feedTable()
  })
})

describe('the live feed', () => {
  it('shows a skeleton until the first response lands', () => {
    stubFetch({ news: () => new Promise<Response>(() => {}) })
    render(<App />)

    expect(within(feedSection()).getByText('Loading news feed')).toBeInTheDocument()
    expect(
      within(feedSection()).queryByRole('columnheader', { name: 'Headline' }),
    ).not.toBeInTheDocument()
  })

  it('renders the server’s rows in the server’s order, with ticker, sector and publisher', async () => {
    render(<App />)
    const table = await feedTable()

    expect(bodyRows(table)).toHaveLength(2)
    expect(column(table, 1)).toEqual(['AAPL', 'MARKET'])
    expect(column(table, 2)[0]).toBe('Apple widens its buyback by $20B')
    expect(column(table, 3)).toEqual(['Other', 'Macro'])
    expect(column(table, 5)[0]).toBe('Reuters')
  })

  it('links each headline to its article, in a new tab with no opener', async () => {
    render(<App />)
    await feedTable()
    const link = within(feedSection()).getByRole('link', { name: /Apple widens its buyback/ })

    expect(link).toHaveAttribute('href', 'https://www.reuters.com/apple-buyback')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  /** The vendor is provenance, not a publisher, and the server does not
   * substitute it — so neither does the page, and it never prints "null". */
  it('renders a missing publisher as a dash, never "null"', async () => {
    render(<App />)
    const table = await feedTable()
    const cell = within(bodyRows(table)[1]).getAllByRole('cell')[5]

    expect(cell.textContent).toContain('—')
    expect(cell.textContent).not.toMatch(/null/i)
    expect(within(cell).getByText('No publisher named')).toHaveClass('sr-only')
  })

  /** No source labelling a headline is the system working, not failing:
   * caution, never error. And no tier name — no tier produced it, so the
   * tier slot is an em dash (decision 17). */
  it('reads Unclassified in caution with an em dash for its tier', async () => {
    render(<App />)
    const table = await feedTable()
    const row = bodyRows(table)[0]
    const label = within(row).getByText('UNCL')

    expect(label).toHaveAttribute('title', 'Unclassified')
    expect(label).toHaveClass('text-caution')
    expect(label).not.toHaveClass('text-error')
    expect(label.closest('td')?.textContent).toContain('—')
    expect(within(row).getByText('No tier')).toHaveClass('sr-only')
    for (const tier of ['Vendor', 'Rules', 'Provider', 'LLM', 'undefined', 'null']) {
      expect(within(table).queryByText(tier)).not.toBeInTheDocument()
    }
  })

  it('asks for the watch list by default, and everything on toggle', async () => {
    render(<App />)
    await feedTable()
    expect(newsRequests()[0].get('scope')).toBe('watch')
    expect(within(feedSection()).getByRole('button', { name: 'Watch list' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.click(within(feedSection()).getByRole('button', { name: 'Everything' }))

    await waitFor(() => expect(newsRequests().at(-1)?.get('scope')).toBe('all'))
  })

  it('passes the lookback, filters and sort to the server rather than filtering locally', async () => {
    render(<App />)
    await feedTable()

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })
    await waitFor(() => expect(newsRequests().at(-1)?.get('lookback')).toBe('today'))

    fireEvent.change(screen.getByLabelText('Sort headlines by time'), {
      target: { value: 'oldest' },
    })
    await waitFor(() => expect(newsRequests().at(-1)?.get('sort')).toBe('oldest'))

    fireEvent.change(screen.getByLabelText('Filter headlines by sector'), {
      target: { value: 'Macro' },
    })
    await waitFor(() => expect(newsRequests().at(-1)?.get('sector')).toBe('Macro'))
  })

  it('pages with offset when the server has more', async () => {
    stubFetch({
      news: (params) =>
        jsonResponse(
          200,
          feed({ total: 60, hasMore: true, offset: Number(params.get('offset') ?? 0) }),
        ),
    })
    render(<App />)
    await feedTable()
    expect(newsRequests()[0].get('offset')).toBe('0')
    expect(newsRequests()[0].get('limit')).toBe('25')

    fireEvent.click(within(feedSection()).getByRole('button', { name: /next/i }))

    await waitFor(() => expect(newsRequests().at(-1)?.get('offset')).toBe('25'))
  })

  it('says the watch list is empty, and offers everything', async () => {
    stubFetch({
      news: (params) =>
        jsonResponse(
          200,
          feed({ items: [], scope: params.get('scope') === 'all' ? 'all' : 'watch' }),
        ),
    })
    render(<App />)

    expect(
      await within(feedSection()).findByText(/No headlines on your watch list yet/),
    ).toBeInTheDocument()

    fireEvent.click(within(feedSection()).getByRole('button', { name: 'Switch to everything' }))

    expect(
      await within(feedSection()).findByText(/No headlines have been stored yet/),
    ).toBeInTheDocument()
    expect(newsRequests().at(-1)?.get('scope')).toBe('all')
  })

  it('words the empty watch list for the lookback', async () => {
    stubFetch({ news: () => jsonResponse(200, feed({ items: [] })) })
    render(<App />)
    await within(feedSection()).findByText(/No headlines on your watch list yet/)

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })

    expect(
      await within(feedSection()).findByText(/No headlines on your watch list today/),
    ).toBeInTheDocument()
  })

  it('renders a failed request as an error, in error', async () => {
    stubFetch({
      news: () => refusal(503, 'news_unavailable', 'The news store could not be read.'),
    })
    render(<App />)

    const alert = await within(feedSection()).findByRole('alert', {}, { timeout: 4000 })
    expect(alert).toHaveTextContent('The news store could not be read.')
    expect(alert).toHaveClass('text-error')
  })

  it('captions the Other sector while the SPDR seed is missing', async () => {
    stubFetch({ news: () => jsonResponse(200, feed({ sectorsAvailable: false, seedAsOf: null })) })
    render(<App />)
    await feedTable()

    expect(
      within(feedSection()).getByText(/Sectors appear once the SPDR holdings seed is built/),
    ).toBeInTheDocument()
  })
})

describe('the watch list', () => {
  it('lists the manual watches against the cap', async () => {
    render(<App />)
    const panel = section('Watch list')

    expect(await within(panel).findByText('PLTR')).toBeInTheDocument()
    expect(within(panel).getByText('SOFI')).toBeInTheDocument()
    expect(within(panel).getByText(/2 of 34 manual watches/)).toBeInTheDocument()
    expect(within(panel).queryByText(/of 100/)).not.toBeInTheDocument()
  })

  it('removes a watch with DELETE and drops the row on success, without a confirm', async () => {
    render(<App />)
    const panel = section('Watch list')
    await within(panel).findByText('PLTR')

    fireEvent.click(within(panel).getByRole('button', { name: 'Remove PLTR from the watch list' }))

    await waitFor(() => expect(within(panel).queryByText('PLTR')).not.toBeInTheDocument())
    expect(calls.some((c) => c.method === 'DELETE' && c.path.endsWith('/news/watch/PLTR'))).toBe(
      true,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(within(panel).getByText('SOFI')).toBeInTheDocument()
  })

  it('adds a watch with POST', async () => {
    render(<App />)
    const panel = section('Watch list')
    await within(panel).findByText('PLTR')

    fireEvent.change(within(panel).getByLabelText('Ticker to watch'), { target: { value: 'hood' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Watch' }))

    expect(await within(panel).findByText('HOOD')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'POST' && c.path.endsWith('/news/watch/HOOD'))).toBe(
      true,
    )
  })

  /** The server validates every add (rule 4) and says why it refused. The
   * page renders that sentence rather than a generic failure. */
  it('renders the server’s refusal inline', async () => {
    stubFetch({
      post: refusal(
        422,
        'not_an_active_us_equity',
        'ZZZZ is not an active US equity in the asset list',
      ),
    })
    render(<App />)
    const panel = section('Watch list')
    await within(panel).findByText('PLTR')

    fireEvent.change(within(panel).getByLabelText('Ticker to watch'), { target: { value: 'ZZZZ' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Watch' }))

    const alert = await within(panel).findByRole('alert')
    expect(alert).toHaveTextContent('ZZZZ is not an active US equity in the asset list')
    expect(alert).toHaveClass('text-error')
  })

  it('says the count is provisional while the seed is missing', async () => {
    served = watchList({ seedMissing: true })
    render(<App />)

    const note = await within(section('Watch list')).findByText(
      /SPDR holdings seed is not built/,
    )
    expect(note).toBeInTheDocument()
    // Q13: the seed moves the universe, never the manual cap.
    expect(note.textContent).toMatch(/cap is unaffected/)
  })
})

describe('the market sentiment composite', () => {
  /** The headline figure and the seven rows beneath it are two renderings
   * of one fact, so they cannot be allowed to disagree. */
  it('renders the derived composite, not a stored one', () => {
    render(<App />)
    const panel = section('Market Sentiment')
    const score = compositeScore(SENTIMENT_COMPONENTS)

    expect(within(panel).getByText(String(score))).toBeInTheDocument()
  })

  it('shows all seven components inline rather than on hover only', () => {
    render(<App />)
    const panel = section('Market Sentiment')

    for (const c of SENTIMENT_COMPONENTS) {
      expect(within(panel).getByText(c.name)).toBeInTheDocument()
    }
  })

  /** Points, not percent: a 0-100 index has no units to be a percentage
   * of. */
  it('reports the one-day move in points', () => {
    render(<App />)
    expect(within(section('Market Sentiment')).getByText(/pts/)).toBeInTheDocument()
  })
})

describe('social attention', () => {
  it('ranks by velocity rather than raw mentions', () => {
    render(<App />)
    const table = within(section('Social Attention')).getByRole('table')
    const tickers = column(table, 0)

    // CRWV is 5x its baseline on far fewer mentions than the mega caps.
    expect(tickers[0]).toBe('CRWV')
    expect(tickers).toHaveLength(SOCIAL_ATTENTION.length)
  })

  it('says it is context and never a trigger', () => {
    render(<App />)
    expect(
      within(section('Social Attention')).getByText(/Context, never a trade trigger/),
    ).toBeInTheDocument()
  })

  /** PRD.md §8.3 asks for both: the sample says how much conversation
   * there was, the labelled count says how much of it the sentiment was
   * actually computed from. A share alone hides the first. */
  it('shows both the sample size and the labelled count', () => {
    render(<App />)
    const table = within(section('Social Attention')).getByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Msgs' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Lbl.' })).toBeInTheDocument()

    // ALAB is the thin-coverage fixture: 88 labels out of 780 messages.
    const alab = bodyRows(table).find((r) => within(r).getAllByRole('cell')[0].textContent === 'ALAB')!
    const cells = within(alab).getAllByRole('cell')
    expect(cells[2].textContent).toBe('780')
    expect(cells[3].textContent).toBe('88')
  })
})

describe('top rated by sector', () => {
  it('lists every sector, ranked by net rating', () => {
    render(<App />)
    const table = within(section('Top rated by sector')).getByRole('table')
    expect(bodyRows(table)).toHaveLength(SECTOR_CONSENSUS.length)
    expect(column(table, 0)[0]).toContain('Communication Services')
  })

  it('states the as-of date, because it refreshes monthly', () => {
    render(<App />)
    expect(within(section('Top rated by sector')).getByText(/as of/i)).toBeInTheDocument()
  })
})

describe('the market calendar', () => {
  /** An ex-dividend date has no 8:30am. Printing a placeholder midnight
   * would invent one and, in ET, put it on the previous evening. */
  it('reads All day for an event that never had a time', () => {
    render(<App />)
    expect(within(section('Market calendar')).getAllByText('All day').length).toBeGreaterThan(0)
  })

  it('carries timed events too', () => {
    render(<App />)
    const panel = section('Market calendar')
    expect(within(panel).getAllByText(/\d{1,2}:\d{2}\s?(AM|PM)/).length).toBeGreaterThan(0)
  })

  it('labels each event with its type', () => {
    render(<App />)
    const panel = section('Market calendar')
    expect(within(panel).getAllByText('Earnings').length).toBeGreaterThan(0)
    expect(within(panel).getAllByText('Central bank').length).toBeGreaterThan(0)
  })
})

/** Phase 2 decision 8, and the Settings precedent once a page is mixed. The
 * feed and the watch list are real since Phase 3 step 4, so a marker on the
 * title would label them invented; each panel that is still a fixture says
 * so itself, and the real ones say nothing. */
describe('the fixture markers', () => {
  const MARKER = 'Sample data — Phase 1'

  it('leaves the title and the real panels unmarked', async () => {
    render(<App />)
    await feedTable()

    const title = screen.getByRole('heading', { level: 1, name: 'News' })
    expect(within(title.parentElement!).queryByText(MARKER)).not.toBeInTheDocument()
    expect(within(feedSection()).queryByText(MARKER)).not.toBeInTheDocument()
    expect(within(section('Watch list')).queryByText(MARKER)).not.toBeInTheDocument()
  })

  it('marks each panel that is still sample data', () => {
    render(<App />)

    for (const name of [
      'Market Sentiment',
      'Social Attention',
      'Top rated by sector',
      'Market calendar',
    ]) {
      expect(within(section(name)).getByText(MARKER)).toBeInTheDocument()
    }
  })

  it('is a status label, not a control', () => {
    render(<App />)
    for (const marker of screen.getAllByText(MARKER)) {
      expect(marker.closest('button')).toBeNull()
    }
  })

  /** On a non-focusable span the tooltip is mouse-only, so the detail rides
   * along for a screen reader in the tooltip's own words. */
  it('puts the composite’s detail in reach without a pointer', () => {
    render(<App />)
    const panel = section('Market Sentiment')
    const detail = within(panel).getByText(/nothing here was computed from a published headline/)

    expect(detail).toHaveClass('sr-only')
    expect(detail.textContent).toBe(within(panel).getByText(MARKER).getAttribute('title'))
  })
})
