import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { SECTOR_CONSENSUS, SENTIMENT_COMPONENTS, SOCIAL_ATTENTION } from '../lib/mockData'
import { compositeScore } from '../lib/news'
import type { CalendarRange, NewsFeed, NewsItem, NewsMover, NewsMovers, WatchList } from '../lib/types'

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
    sourceStatus: null,
    otherLabel: null,
    attributedBy: 'tag',
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

/** `GET /api/calendar`, one manual row. The panel's own states are tested
 * in `components/MarketCalendar.test.tsx`; this only proves the page
 * composes the real panel. */
const CALENDAR: CalendarRange = {
  start: '2026-10-12',
  end: '2026-11-08',
  maxSpanDays: 92,
  days: [
    {
      date: '2026-10-16',
      events: [
        {
          id: '9',
          date: '2026-10-16',
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
        },
      ],
    },
  ],
  total: 1,
  notices: {
    seedGaps: [],
    jobs: [],
    releaseFigures: { state: 'pending_owner_decision', reason: 'Pending an owner decision.' },
  },
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

/** `corollary/api/schemas.py`'s `NewsMover`, camelCased. Two rows by
 * default, deliberately **not** in alphabetical or recency order — a rules
 * row (HOOD) ahead of a newer Massive-only one (ACME) — so a client that
 * re-sorted would visibly reorder them. */
function mover(overrides: Partial<NewsMover> = {}): NewsMover {
  return {
    ticker: 'HOOD',
    sector: 'Other',
    rankGroup: 'rules',
    reasons: [
      {
        source: 'rules',
        sourceStatus: 'unaudited',
        tier: 'rules',
        ruleId: 'earnings_beat',
        family: 'earnings',
        direction: 'bullish',
        articleIds: [501],
        evidence: [{ articleId: 501, time: '2026-09-25T13:40:00Z', reasoning: 'beats estimates' }],
        latestAt: '2026-09-25T13:40:00Z',
      },
    ],
    directions: ['bullish'],
    articles: [
      {
        id: 501,
        time: '2026-09-25T13:40:00Z',
        headline: 'Robinhood beats estimates on deposit growth',
        url: 'https://example.com/hood-beat',
        publisher: 'Benzinga',
      },
    ],
    articleCount: 1,
    latestAt: '2026-09-25T13:40:00Z',
    avgVolume: 48_200_000,
    advSessions: 20,
    lastClose: '9.70',
    tradeabilitySessionDate: '2026-09-24',
    tradeabilityCheckedAt: '2026-09-25T12:30:00Z',
    ...overrides,
  }
}

const ACME = mover({
  ticker: 'ACME',
  rankGroup: 'massive_only',
  reasons: [
    {
      source: 'massive',
      sourceStatus: 'unaudited',
      tier: 'vendor',
      ruleId: null,
      family: null,
      direction: 'bearish',
      articleIds: [612],
      evidence: [
        { articleId: 612, time: '2026-09-25T14:10:00Z', reasoning: 'Guidance cut on weak demand.' },
      ],
      latestAt: '2026-09-25T14:10:00Z',
    },
  ],
  directions: ['bearish'],
  articles: [
    {
      id: 612,
      time: '2026-09-25T14:10:00Z',
      headline: 'Acme trims its outlook',
      url: 'https://example.com/acme-outlook',
      publisher: null,
    },
  ],
  latestAt: '2026-09-25T14:10:00Z',
  avgVolume: 812_000,
  advSessions: 7,
  // Trailing zeros a float round-trip would drop.
  lastClose: '41.200',
})

function movers(overrides: Partial<NewsMovers> = {}): NewsMovers {
  return {
    items: [mover(), ACME],
    lookback: 'all',
    since: null,
    evaluatedAt: '2026-09-25T14:30:00Z',
    signalled: 3,
    watchedExcluded: 1,
    failedTradeability: 1,
    awaitingCheck: 0,
    feeds: [
      {
        feed: 'alpaca_news',
        job: 'alpaca_news_poll',
        scheduled: true,
        lastSuccess: '2026-09-25T14:29:00Z',
        lastFailure: null,
        failing: false,
        lastIngestedAt: '2026-09-25T14:29:00Z',
        stale: false,
        staleSince: null,
      },
    ],
    feedsStale: false,
    staleSince: null,
    evaluation: {
      job: 'tradeability_refresh',
      scheduled: true,
      lastSuccess: '2026-09-25T12:30:00Z',
      lastFailure: null,
      failing: false,
      lastCheckedAt: '2026-09-25T12:30:00Z',
    },
    sectorsAvailable: true,
    ...overrides,
  }
}

interface Stub {
  /** Answers `GET /api/news`, given the request's query. */
  news?: (params: URLSearchParams) => Response | Promise<Response>
  post?: Response
  /** Answers `GET /api/news/movers`. Defaults to `movers()` less any
   * ticker now on the manual watch list, the way the server excludes it. */
  movers?: (params: URLSearchParams) => Response | Promise<Response>
}

let served: WatchList
let calls: { method: string; path: string; params: URLSearchParams }[]

function stubFetch(stub: Stub = {}) {
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), 'http://127.0.0.1')
    const method = init?.method ?? 'GET'
    const path = url.pathname
    calls.push({ method, path, params: url.searchParams })

    if (path.endsWith('/calendar')) return Promise.resolve(jsonResponse(200, CALENDAR))
    if (path.endsWith('/calendar')) return Promise.resolve(jsonResponse(200, CALENDAR))
    if (path.endsWith('/news/movers')) {
      if (stub.movers) return Promise.resolve(stub.movers(url.searchParams))
      const watched = new Set(served.manual.map((m) => m.ticker))
      const body = movers()
      return Promise.resolve(
        jsonResponse(200, { ...body, items: body.items.filter((m) => !watched.has(m.ticker)) }),
      )
    }
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
  /** Step 7: the panel reads `GET /api/calendar`, not the fixture. */
  it('renders the engine’s rows', async () => {
    render(<App />)
    const panel = section('Market calendar')
    expect(await within(panel).findByText('G20 summit')).toBeInTheDocument()
    expect(calls.some((c) => c.method === 'GET' && c.path.endsWith('/calendar'))).toBe(true)
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
    await within(section('Market calendar')).findByText('G20 summit')
    expect(within(section('Market calendar')).queryByText(MARKER)).not.toBeInTheDocument()
  })

  it('marks each panel that is still sample data', () => {
    render(<App />)

    for (const name of [
      'Market Sentiment',
      'Social Attention',
      'Top rated by sector',
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

/** Phase 3 step 5: the feed carries real labels. The server's invariants
 * hold in every stub here — tier, source and sourceStatus are null exactly
 * when the item is unclassified, and every source is `unaudited` until
 * step 6 runs an audit. */
describe('the feed’s labels', () => {
  const RULES = item({
    id: '201:NVDA',
    ticker: 'NVDA',
    headline: 'Nvidia beats estimates and raises guidance',
    sentiment: 'bullish',
    tier: 'rules',
    source: 'rules',
    sourceStatus: 'unaudited',
  })
  const VENDOR = item({
    id: '202:TSLA',
    ticker: 'TSLA',
    headline: 'Tesla deliveries slip',
    sentiment: 'bearish',
    tier: 'vendor',
    source: 'massive',
    sourceStatus: 'unaudited',
  })

  function serve(items: NewsItem[]) {
    stubFetch({ news: () => jsonResponse(200, feed({ items })) })
  }

  it('names the tier and the source, the direction in its own colour', async () => {
    serve([RULES, VENDOR])
    render(<App />)
    const [rules, vendor] = bodyRows(await feedTable())

    expect(within(rules).getByText('BULL')).toHaveClass('text-bullish')
    expect(within(rules).getByText(/^Rules/)).toBeInTheDocument()
    expect(within(rules).getByText(/Corollary rules/)).toHaveClass('sr-only')
    expect(within(vendor).getByText('BEAR')).toHaveClass('text-bearish')
    expect(within(vendor).getByText('Vendor · Massive')).toBeInTheDocument()
  })

  it('renders a null tier as an em dash and Unclassified in caution, never error', async () => {
    serve([item(), RULES])
    render(<App />)
    const [unlabelled] = bodyRows(await feedTable())

    expect(within(unlabelled).getByText('UNCL')).toHaveClass('text-caution')
    expect(within(unlabelled).getByText('UNCL')).not.toHaveClass('text-error')
    expect(within(unlabelled).getByText('—')).toBeInTheDocument()
    expect(within(unlabelled).getByText('No tier')).toHaveClass('sr-only')
  })

  it('says when the ticker came from the headline rather than a tag', async () => {
    serve([{ ...RULES, attributedBy: 'headline' }, VENDOR])
    render(<App />)
    const [named, tagged] = bodyRows(await feedTable())

    expect(within(named).getByText('named in headline')).toBeInTheDocument()
    expect(within(tagged).queryByText('named in headline')).not.toBeInTheDocument()
  })

  /** Q22: `unaudited` is not "not demoted", and the suffix keys on
   * `demoted` alone — so it cannot appear before step 6 audits anything. */
  it('shows no demoted suffix while the source is unaudited, and shows it once demoted', async () => {
    serve([RULES, { ...VENDOR, sourceStatus: 'demoted' }])
    render(<App />)
    const [unaudited, demoted] = bodyRows(await feedTable())

    expect(within(unaudited).queryByText(/demoted/)).not.toBeInTheDocument()
    expect(within(unaudited).queryByText(/display only/)).not.toBeInTheDocument()
    expect(within(demoted).getByText(/\(demoted · display only\)/)).toBeInTheDocument()
  })

  it('puts the non-displayed label on hover and on expand, without replacing the shown one', async () => {
    serve([
      {
        ...RULES,
        otherLabel: {
          sentiment: 'bearish',
          tier: 'vendor',
          source: 'massive',
          sourceStatus: 'unaudited',
          ruleId: null,
          reasoning: 'Margins compress on mix.',
        },
      },
    ])
    render(<App />)
    const table = await feedTable()
    const [row] = bodyRows(table)
    const toggle = within(row).getByRole('button', { name: /Massive’s label, not displayed/ })

    expect(toggle).toHaveAttribute('title', 'Massive labelled this Bearish: Margins compress on mix.')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(row).getByText('BULL')).toHaveClass('text-bullish')

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(within(table).getByText(/“Margins compress on mix\.”/)).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(within(table).queryByText(/“Margins compress on mix\.”/)).not.toBeInTheDocument()
  })

  /** A `title` reaches no keyboard, touch or screen-reader user, so the
   * unaudited state is said in a visible line — on the feed and on the
   * movers panel — and only while the served status says so. */
  it('says visibly that the labels are unaudited, on the feed and the movers panel', async () => {
    serve([RULES, VENDOR])
    render(<App />)
    await feedTable()

    const caption = 'Labels are unaudited: no accuracy audit has run yet.'
    expect(within(feedSection()).getByText(caption)).toHaveClass('text-on-surface-variant')
    const movers = section('Movers in the news')
    expect(await within(movers).findByText(caption)).toHaveClass('text-on-surface-variant')
  })

  it('drops the unaudited line once no served label is unaudited', async () => {
    const audited = (m: NewsMover): NewsMover => ({
      ...m,
      reasons: m.reasons.map((r) => ({ ...r, sourceStatus: 'active' as const })),
    })
    stubFetch({
      news: () =>
        jsonResponse(
          200,
          feed({
            items: [
              { ...RULES, sourceStatus: 'active' },
              { ...VENDOR, sourceStatus: 'active' },
            ],
          }),
        ),
      movers: () => jsonResponse(200, movers({ items: [audited(mover()), audited(ACME)] })),
    })
    render(<App />)
    await feedTable()
    await within(section('Movers in the news')).findByRole('list', { name: 'Movers' })

    expect(screen.queryByText(/unaudited/)).not.toBeInTheDocument()
  })

  /** The movers caption is the panel's own: under the Unclassified filter, a
   * quiet watch list or a feed error the feed carries no label at all, and
   * borrowing from it dropped the caption over rows that are all unaudited. */
  it('keeps the movers caption when the feed carries no label', async () => {
    serve([item()])
    render(<App />)
    await feedTable()

    expect(within(feedSection()).queryByText(/unaudited/)).not.toBeInTheDocument()
    const moversPanel = section('Movers in the news')
    expect(
      await within(moversPanel).findByText('Labels are unaudited: no accuracy audit has run yet.'),
    ).toHaveClass('text-on-surface-variant')
  })

  it('keeps the movers caption when the feed request fails', async () => {
    stubFetch({ news: () => refusal(503, 'news_unavailable', 'The news store could not be read.') })
    render(<App />)

    const moversPanel = section('Movers in the news')
    expect(
      await within(moversPanel).findByText('Labels are unaudited: no accuracy audit has run yet.'),
    ).toBeInTheDocument()
  })

  /** Q22: a rules-only feed beside a Massive-only mover. Borrowed from the
   * feed, the panel's caption named rules alone and implied Massive passed. */
  it('names Massive on the movers caption beside a rules-only feed', async () => {
    stubFetch({
      news: () => jsonResponse(200, feed({ items: [RULES] })),
      movers: () => jsonResponse(200, movers({ items: [ACME] })),
    })
    render(<App />)
    await feedTable()

    expect(
      within(feedSection()).getByText(
        'Corollary rules labels are unaudited: no accuracy audit has run on that source yet.',
      ),
    ).toBeInTheDocument()
    const moversPanel = section('Movers in the news')
    expect(
      await within(moversPanel).findByText(
        'Massive labels are unaudited: no accuracy audit has run on that source yet.',
      ),
    ).toBeInTheDocument()
    expect(within(moversPanel).queryByText(/Corollary rules labels/)).not.toBeInTheDocument()
  })

  it('names only the unaudited source when the other has been audited', async () => {
    serve([{ ...RULES, sourceStatus: 'active' }, VENDOR])
    render(<App />)
    await feedTable()

    expect(
      within(feedSection()).getByText(
        'Massive labels are unaudited: no accuracy audit has run on that source yet.',
      ),
    ).toBeInTheDocument()
  })

  /** An unrecognised status used to fall through to "passed its audit". */
  it('claims an audit pass only for active, and nothing for an unknown status', async () => {
    serve([
      { ...RULES, sourceStatus: 'active' },
      { ...VENDOR, sourceStatus: 'under_review' as NewsItem['sourceStatus'] },
    ])
    render(<App />)
    const [active, unknown] = bodyRows(await feedTable())

    expect(within(active).getByText(/^Rules/)).toHaveAttribute(
      'title',
      expect.stringContaining('This source passed its accuracy audit.'),
    )
    const tag = within(unknown).getByText('Vendor · Massive')
    expect(tag.getAttribute('title')).toMatch(/Source: Massive\.$/)
    expect(tag.getAttribute('title')).not.toMatch(/passed|audit/)
  })
})

/** Decision 21's discovery panel, from `GET /api/news/movers`. */
describe('movers in the news', () => {
  function panel(): HTMLElement {
    return section('Movers in the news')
  }

  async function moverItems(): Promise<HTMLElement[]> {
    const list = await within(panel()).findByRole('list', { name: 'Movers' })
    return within(list)
      .getAllByRole('listitem')
      .filter((li) => li.parentElement === list)
  }

  function tickers(rows: HTMLElement[]): (string | null | undefined)[] {
    return rows.map((r) => r.querySelector('.text-label-md')?.textContent)
  }

  function moversRequests(): URLSearchParams[] {
    return calls
      .filter((c) => c.method === 'GET' && c.path.endsWith('/news/movers'))
      .map((c) => c.params)
  }

  it('shows a skeleton until the first response lands', () => {
    stubFetch({ movers: () => new Promise<Response>(() => {}) })
    render(<App />)

    expect(within(panel()).getByText('Loading movers in the news')).toBeInTheDocument()
  })

  it('is captioned as flagged by news, not confirmed by price, and carries no marker', async () => {
    render(<App />)
    await moverItems()

    expect(within(panel()).getByText(/Flagged by news, not confirmed by price/)).toBeInTheDocument()
    expect(within(panel()).queryByText('Sample data — Phase 1')).not.toBeInTheDocument()
  })

  it('keeps the server’s order rather than re-sorting', async () => {
    render(<App />)

    // HOOD first though ACME is newer and sorts first by name: rules rows
    // rank ahead of Massive-only ones, and that ranking is the server's.
    expect(tickers(await moverItems())).toEqual(['HOOD', 'ACME'])
  })

  it('names each reason in words, with its direction', async () => {
    render(<App />)
    const [hood, acme] = await moverItems()

    const hoodReasons = within(hood).getByRole('list', { name: 'Why HOOD was flagged' })
    expect(hoodReasons).toHaveTextContent('Earnings Bullish · 1 article')
    const acmeReasons = within(acme).getByRole('list', { name: 'Why ACME was flagged' })
    expect(acmeReasons).toHaveTextContent(
      'Massive Bearish · 1 article — “Guidance cut on weak demand.”',
    )
    expect(within(acmeReasons).getByText('Bearish')).toHaveClass('text-bearish')
  })

  it('shows conflicting directions side by side, never netted', async () => {
    const split = mover({
      reasons: [mover().reasons[0], { ...ACME.reasons[0], direction: 'bearish' }],
      directions: ['bearish', 'bullish'],
    })
    stubFetch({ movers: () => jsonResponse(200, movers({ items: [split] })) })
    render(<App />)
    const [row] = await moverItems()

    // A group, so the label is announced: `aria-label` on a bare span is not.
    const pills = within(row).getByRole('group', { name: 'Reasons disagree: Bearish and Bullish' })
    expect(within(pills).getByText('Bearish')).toHaveClass('text-on-bearish-container')
    expect(within(pills).getByText('Bullish')).toHaveClass('text-on-bullish-container')
    expect(within(row).queryByText(/mixed/i)).not.toBeInTheDocument()
  })

  it('prints the last close from the server’s decimal string, verbatim and in mono', async () => {
    render(<App />)
    const [hood, acme] = await moverItems()

    // '41.200' through parseFloat would print 41.2.
    const close = within(acme).getByText('$41.200')
    expect(close).toHaveClass('font-mono', 'tabular-nums')
    expect(within(hood).getByText('$9.70')).toBeInTheDocument()
    expect(within(acme).getByText('812K')).toHaveClass('font-mono', 'tabular-nums')
    expect(acme).toHaveTextContent('over 7 sessions')
    // A bare session date, formatted in UTC: the 24th, not the 23rd.
    expect(hood).toHaveTextContent('checked for Thu, Sep 24')
  })

  it('links each article, in a new tab with no opener', async () => {
    render(<App />)
    const [hood] = await moverItems()

    const link = within(hood).getByRole('link', { name: 'Robinhood beats estimates on deposit growth' })
    expect(link).toHaveAttribute('href', 'https://example.com/hood-beat')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('says no off-watch-list name passed since the lookback began, and why', async () => {
    stubFetch({
      movers: () =>
        jsonResponse(
          200,
          movers({
            items: [],
            lookback: 'today',
            since: '2026-09-25T04:00:00Z',
            signalled: 3,
            failedTradeability: 1,
            awaitingCheck: 2,
          }),
        ),
    })
    render(<App />)

    expect(
      await within(panel()).findByText(
        'No off-watch-list name passed the filter since Sep 25, 12:00 AM ET.',
      ),
    ).toBeInTheDocument()
    expect(
      within(panel()).getByText(
        '3 signalled: 1 failed the tradeability filter, 2 awaiting a first check.',
      ),
    ).toBeInTheDocument()
    expect(within(panel()).queryByText(/stale/)).not.toBeInTheDocument()
  })

  it('says the feeds are stale instead, which is a different finding', async () => {
    stubFetch({
      movers: () =>
        jsonResponse(200, movers({ items: [], feedsStale: true, staleSince: '2026-09-25T13:15:00Z' })),
    })
    render(<App />)

    const line = await within(panel()).findByText('Discovery feeds stale since Sep 25, 9:15 AM ET.')
    expect(line).toHaveClass('text-caution')
    expect(line).not.toHaveClass('text-error')
    expect(within(panel()).queryByText(/passed the filter/)).not.toBeInTheDocument()
  })

  it('keeps the rows under a stale feed and says the list may be missing names', async () => {
    stubFetch({
      movers: () =>
        jsonResponse(200, movers({ feedsStale: true, staleSince: '2026-09-25T13:15:00Z' })),
    })
    render(<App />)

    expect(await moverItems()).toHaveLength(2)
    expect(
      within(panel()).getByText(
        'Discovery feeds stale since Sep 25, 9:15 AM ET — this list may be missing names.',
      ),
    ).toBeInTheDocument()
  })

  it('keeps the rows when a refetch fails, and says they may be stale', async () => {
    let answered = 0
    stubFetch({
      movers: () => {
        answered += 1
        return answered === 1
          ? jsonResponse(200, movers())
          : refusal(503, 'news_unavailable', 'The news store could not be read.')
      },
    })
    render(<App />)
    await moverItems()

    await queryClient.refetchQueries({ queryKey: ['news', 'movers'] })

    expect(await within(panel()).findByText('The news store could not be read.')).toHaveClass(
      'text-error',
    )
    // Dated, not just clocked: a refetch failing for a day would otherwise
    // read as evaluated this morning.
    expect(
      within(panel()).getByText('These rows may be stale — last evaluated Sep 25, 10:30 AM ET.'),
    ).toBeInTheDocument()
    expect(await moverItems()).toHaveLength(2)
  })

  it('follows the page’s lookback', async () => {
    render(<App />)
    await moverItems()
    expect(moversRequests().at(-1)?.get('lookback')).toBe('all')

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })

    await waitFor(() => expect(moversRequests().at(-1)?.get('lookback')).toBe('today'))
  })

  it('watches with one POST and no confirm, and the row leaves on success', async () => {
    render(<App />)
    const [hood] = await moverItems()

    fireEvent.click(within(hood).getByRole('button', { name: 'Watch HOOD' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(async () => expect(tickers(await moverItems())).toEqual(['ACME']))
    expect(
      calls.filter((c) => c.method === 'POST' && c.path.endsWith('/news/watch/HOOD')),
    ).toHaveLength(1)
    // The watch list picked it up from the same response.
    const watches = within(section('Watch list')).getByRole('list', { name: 'Manual watches' })
    expect(within(watches).getByText('HOOD')).toBeInTheDocument()
  })

  it('keeps the row and renders the refusal when the watch fails', async () => {
    stubFetch({
      post: refusal(409, 'watch_cap_reached', 'The manual-watch cap of 34 is reached.'),
    })
    render(<App />)
    const [hood] = await moverItems()

    fireEvent.click(within(hood).getByRole('button', { name: 'Watch HOOD' }))

    const refusalText = await within(panel()).findByText('The manual-watch cap of 34 is reached.')
    expect(refusalText).toHaveClass('text-error')
    expect(tickers(await moverItems())).toEqual(['HOOD', 'ACME'])
  })

  /** The article line is the only place a row says when its news ran: under
   * a multi-day lookback the clock alone dates old news to this morning, and
   * under `all` — labelled articles are kept indefinitely — the year rides
   * too, or "Sep 25" could be any September. */
  it('dates each article with its year under all, its date under a bounded lookback, and the clock alone under today', async () => {
    render(<App />)
    const [hood] = await moverItems()
    expect(within(hood).getByRole('list', { name: 'Articles on HOOD' })).toHaveTextContent(
      'Fri, Sep 25, 2026, 9:40 AM Robinhood beats estimates',
    )

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: '1w' },
    })
    await waitFor(() =>
      expect(within(panel()).getByRole('list', { name: 'Articles on HOOD' })).toHaveTextContent(
        /^Sep 25, 9:40 AM Robinhood beats estimates/,
      ),
    )

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })

    await waitFor(() =>
      expect(within(panel()).getByRole('list', { name: 'Articles on HOOD' })).toHaveTextContent(
        /^9:40 AM Robinhood beats estimates/,
      ),
    )
  })

  it('says sectors are pending when the seed is missing, rather than a column of Other', async () => {
    stubFetch({ movers: () => jsonResponse(200, movers({ sectorsAvailable: false })) })
    render(<App />)
    await moverItems()

    expect(within(panel()).getByText(/Sectors appear once the SPDR holdings seed is built/)).toHaveClass(
      'text-on-surface-variant',
    )
  })

  it('says nothing about sectors while the seed is built', async () => {
    render(<App />)
    await moverItems()

    expect(within(panel()).queryByText(/SPDR holdings seed/)).not.toBeInTheDocument()
  })

  it('names a failing tradeability job in the none-passed state, in caution', async () => {
    stubFetch({
      movers: () =>
        jsonResponse(
          200,
          movers({
            items: [],
            signalled: 2,
            failedTradeability: 0,
            awaitingCheck: 2,
            evaluation: {
              job: 'tradeability_refresh',
              scheduled: true,
              lastSuccess: '2026-09-24T12:30:00Z',
              lastFailure: '2026-09-25T12:30:00Z',
              failing: true,
              lastCheckedAt: '2026-09-24T12:30:00Z',
            },
          }),
        ),
    })
    render(<App />)

    const line = await within(panel()).findByText(
      'The tradeability check (tradeability_refresh) is failing — it last succeeded Sep 24, 8:30 AM ET — so signalled names are not being checked.',
    )
    expect(line).toHaveClass('text-caution')
    expect(line).not.toHaveClass('text-error')
  })

  it('names an unscheduled tradeability job in the none-passed state', async () => {
    stubFetch({
      movers: () =>
        jsonResponse(
          200,
          movers({
            items: [],
            awaitingCheck: 2,
            evaluation: { ...movers().evaluation, scheduled: false },
          }),
        ),
    })
    render(<App />)

    expect(
      await within(panel()).findByText(
        'The tradeability check (tradeability_refresh) is not scheduled, so signalled names are not being checked.',
      ),
    ).toHaveClass('text-caution')
  })

  it('adds no job line while the tradeability check is healthy', async () => {
    stubFetch({ movers: () => jsonResponse(200, movers({ items: [] })) })
    render(<App />)
    await within(panel()).findByText(/passed the filter/)

    expect(within(panel()).queryByText(/tradeability check/)).not.toBeInTheDocument()
  })

  /** The optimistic drop moves the counts with the row, so watching the
   * last mover never shows an empty state built from stale counts. */
  it('counts the watched name out of signalled when the last mover is watched', async () => {
    let answered = 0
    stubFetch({
      movers: () => {
        answered += 1
        // The refetch after the watch never answers, so what is on screen is
        // the optimistic update alone.
        return answered === 1
          ? jsonResponse(
              200,
              movers({ items: [mover()], signalled: 2, failedTradeability: 1, watchedExcluded: 0 }),
            )
          : new Promise<Response>(() => {})
      },
    })
    render(<App />)
    const [hood] = await moverItems()

    fireEvent.click(within(hood).getByRole('button', { name: 'Watch HOOD' }))

    expect(
      await within(panel()).findByText('1 signalled: 1 failed the tradeability filter.'),
    ).toBeInTheDocument()
  })
})
