import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { MARKETS_FOREGROUND_POLL_MS } from '../hooks/useMarketPoll'
import { MARKETS_VIEWPORT_DEBOUNCE_MS } from '../lib/markets'
import { queryClient } from '../lib/queryClient'
import { refetchStocks } from '../lib/queries'
import { useUIStore } from '../lib/store'
import type { AccountResponse, OptionContract, RiskLimit, StockQuote } from '../lib/types'

/** Step 15 (b) sends through `liveSocket.ts`'s module-level export and owns
 * no socket code of its own — the boundary (a) landed with. Spied rather
 * than stubbed out entirely: what is under test is *what* is sent and *how
 * often*, which is the whole of (b).
 *
 * `true` is the default return, meaning "it reached the socket". It is not
 * an acceptance — there is no acknowledgement frame — and the refusal test
 * below exercises the other path. */
const { sendMarketsVisible } = vi.hoisted(() => ({
  sendMarketsVisible: vi.fn<(symbols: readonly string[]) => boolean>(() => true),
}))

vi.mock('../lib/liveSocket', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/liveSocket')>()),
  sendMarketsVisible,
}))

/** Markets reads three market endpoints and the account, and writes none of
 * them. Every payload below is the shape
 * `http://127.0.0.1:5173/api/markets/*` actually answers with — including
 * the nulls, which are the point: a live chain has no bid on roughly half
 * its contracts, a fund has no market capitalisation, and open interest is
 * absent wherever the vendor did not carry it (decision 15).
 *
 * **The stock rows carry `previousClose` and no change**, because the wire
 * does not carry one any more (decision 18, rule 4): a change is derived
 * from the price on screen by `changeOf` / `changePctOf`, so the two
 * numbers cannot disagree. These fixtures are the basis and the price; the
 * moves quoted in the comments are what the table derives from them.
 */
const STOCKS: StockQuote[] = [
  {
    symbol: 'NVDA',
    name: 'NVIDIA Corp.',
    price: 218.17,
    at: '2026-08-07T19:45:00Z',
    // 218.17 − 218.37 = −0.20, −0.09% — down on the day, as the recording
    // this fixture came from was.
    previousClose: 218.37,
    // Still running: this is what has traded so far today.
    volume: 120_000_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-11',
    avgVolume: 126_161_848,
    marketCap: 5_300,
  },
  {
    symbol: 'SPY',
    name: 'SPDR S&P 500 ETF Trust',
    price: 764.285,
    at: '2026-08-07T19:45:00Z',
    // 764.285 − 757.87 = +6.415, +0.85%. The half-cent is deliberate and is
    // the case `changeOf`'s snap exists for: in doubles the subtraction
    // yields 6.414999999999964, which would render `+$6.41` where the exact
    // figure renders `+$6.42`.
    previousClose: 757.87,
    // A finished session, and the most recent one on the table.
    volume: 40_000_000,
    volumeSession: 'completed',
    volumeDate: '2026-09-11',
    avgVolume: 40_407_880,
    // A fund has none. Never 0.
    marketCap: null,
  },
  {
    symbol: 'RDDT',
    name: 'Reddit Inc.',
    price: 200,
    at: '2026-08-07T19:45:00Z',
    // No prior daily bar: no basis, so there is no move to state. Null,
    // never 0 — a `+$0.00` would claim the price was unchanged.
    previousClose: null,
    // Completed, but a week behind everyone else — this symbol stopped
    // printing and the column has to say so.
    volume: 5_000_000,
    volumeSession: 'completed',
    volumeDate: '2026-09-04',
    avgVolume: 10_000_000,
    marketCap: 35,
  },
  {
    symbol: 'ZZZ',
    name: 'Quiet Holdings',
    price: 5,
    at: '2026-08-07T19:45:00Z',
    // 5 − 4 = +1.00, +25.00%. The pair this replaced was `change: 1,
    // changePct: 2` on a price of 5, which no previous close can produce:
    // +$1.00 on a $5 stock is +25%, not +2%. Nothing caught it because the
    // two fields were stored independently — exactly the drift rule 4
    // removes by deriving both from one basis.
    previousClose: 4,
    // No daily bar anywhere in the window. Not a zero.
    volume: null,
    volumeSession: null,
    volumeDate: null,
    avgVolume: null,
    marketCap: 1,
  },
]

const CHAIN: OptionContract[] = [
  {
    symbol: 'NVDA260914C00210000',
    strike: 210,
    expiration: '2026-09-14',
    type: 'call',
    last: 8.38,
    previousClose: 8.95,
    change: -0.57,
    changePct: -6.37,
    bid: 8.15,
    ask: 8.44,
    volume: 1_741,
    openInterest: 663,
    iv: 0.326944,
    // Served, and deliberately not declared in types.ts.
    ivSource: 'derived',
  } as OptionContract,
  {
    symbol: 'NVDA260914C00215000',
    strike: 215,
    expiration: '2026-09-14',
    type: 'call',
    last: 3.8,
    previousClose: 4.85,
    change: -1.05,
    changePct: -21.65,
    bid: 3.68,
    ask: 3.8,
    volume: 1_485,
    // A real zero: nobody holds this contract. Different from absent.
    openInterest: 0,
    iv: 0.2443,
    ivSource: 'vendor',
  } as OptionContract,
  {
    symbol: 'NVDA260914P00220000',
    strike: 220,
    expiration: '2026-09-14',
    type: 'put',
    // Never traded, never quoted. Half a real ladder looks like this.
    last: null,
    previousClose: null,
    change: null,
    changePct: null,
    bid: null,
    ask: null,
    volume: null,
    openInterest: null,
    iv: null,
  },
]

const ACCOUNT: AccountResponse = {
  mode: 'paper',
  equity: 99_901.08,
  cash: 61_119.36,
  buyingPower: 61_119.36,
  positionsValue: 38_781.72,
  dayPnl: 12.5,
  dayPnlPct: 0.01,
  cashAccountUnavailableReason: null,
  missingLiveCredentialEnvVars: [],
} as unknown as AccountResponse

const LIMITS: RiskLimit[] = [
  {
    key: 'max_risk_per_trade_pct',
    label: 'Max risk per trade',
    value: 7,
    unit: '%',
    min: 1,
    max: 25,
    help: 'Ceiling on what one position may lose, as a share of account equity.',
  } as RiskLimit,
]

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

interface Overrides {
  /** A function rather than a value when the answer has to *change* between
   * reads — the stock endpoint is polled, so "the third poll fails" is a
   * state the fixtures have to be able to express. */
  stocks?: Response | (() => Response)
  chain?: Response
  account?: Response
}

let chainCalls: string[] = []

function serve(overrides: Overrides = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)

    if (url.includes('/markets/stocks')) {
      const served = overrides.stocks
      return Promise.resolve(
        typeof served === 'function' ? served() : (served ?? jsonResponse(200, STOCKS)),
      )
    }
    if (url.includes('/markets/chain/')) {
      chainCalls.push(url)
      return Promise.resolve(overrides.chain ?? jsonResponse(200, CHAIN))
    }
    if (url.includes('/markets/underlyings')) {
      return Promise.resolve(jsonResponse(200, []))
    }
    if (url.includes('/settings/limits')) {
      return Promise.resolve(jsonResponse(200, LIMITS))
    }
    if (url.includes('/account')) {
      return Promise.resolve(overrides.account ?? jsonResponse(200, ACCOUNT))
    }
    // Nothing else on this page fetches. A 200 with an empty body keeps an
    // unexpected call from reading as the failure under test.
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/markets')
  // Rule 5: every start is Paper.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
  chainCalls = []
})

/** Each section is a named landmark, so the two tables can be told apart —
 * on the page as much as in the test, where "Symbol" and "Change %" would
 * otherwise match twice. */
function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

function stockTable(): HTMLElement {
  return within(section('Stocks & ETFs')).getByRole('table')
}

function chainTable(): HTMLElement {
  return within(section('Options chains')).getByRole('table')
}

function bodyRows(table: HTMLElement): HTMLElement[] {
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .filter((r) => within(r).queryAllByRole('cell').length > 1)
}

function rowFor(table: HTMLElement, symbol: string): HTMLElement {
  const row = bodyRows(table).find((r) => within(r).queryAllByRole('cell')[0].textContent === symbol)
  if (!row) throw new Error(`no row for ${symbol}`)
  return row
}

function cellText(row: HTMLElement, index: number): string {
  return within(row).getAllByRole('cell')[index].textContent ?? ''
}

async function openChain(symbol: string) {
  const box = within(section('Options chains')).getByLabelText('Search underlying')
  fireEvent.focus(box)
  fireEvent.change(box, { target: { value: symbol } })
  // mouseDown, not Enter: the listbox commits before the input loses focus,
  // which is what keeps a click from being eaten by the outside-click close.
  fireEvent.mouseDown(await screen.findByRole('option', { name: symbol }))
  return within(await screen.findByRole('region', { name: 'Options chains' })).findByRole('table')
}

describe('the stock table reads the served universe', () => {
  it('renders one row per served symbol', async () => {
    serve()
    render(<App />)

    await screen.findByText('NVIDIA Corp.')
    expect(bodyRows(stockTable())).toHaveLength(STOCKS.length)
  })

  it('says which session the volume column is counting, per row', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // A column that silently means either "traded so far this morning" or
    // "all of last Thursday" makes a busy stock read as quiet.
    expect(cellText(rowFor(stockTable(), 'NVDA'), 5)).toContain('so far')
    expect(cellText(rowFor(stockTable(), 'SPY'), 5)).toContain('full session')
  })

  it('dates a completed session that is not the latest one on the table', async () => {
    serve()
    render(<App />)
    await screen.findByText('Reddit Inc.')

    // Sep 4 against a table whose latest print is Sep 11: visibly stale
    // rather than quietly old. The date formats in UTC — in ET a bare
    // YYYY-MM-DD renders as the day before.
    expect(cellText(rowFor(stockTable(), 'RDDT'), 5)).toContain('Sep 4')
  })

  it('renders an absent volume as an absence, never as a zero', async () => {
    serve()
    render(<App />)
    await screen.findByText('Quiet Holdings')

    const volume = cellText(rowFor(stockTable(), 'ZZZ'), 5)
    expect(volume).toContain('—')
    expect(volume).not.toMatch(/\b0\b/)
    // And no relative volume either: there is no numerator, and the
    // denominator is missing too.
    expect(cellText(rowFor(stockTable(), 'ZZZ'), 6)).toContain('—')
  })

  it('survives a server too old to send the volume session at all', async () => {
    // Not a hypothetical: this blanked the page. `volumeSession` and
    // `volumeDate` arrived in a later commit than the running API server, so
    // the rows came back *without the keys*. The guard read `=== null`, which
    // `undefined` walks straight past, and `formatExpiry(undefined)` threw
    // `RangeError: Invalid time value` and unmounted the whole table -- an
    // empty page with nothing on it saying why.
    //
    // The fixtures could not catch it because every one of them is complete.
    // Only a *missing* key reproduces it, so this test builds one by deleting
    // the fields rather than by setting them null, which is already covered.
    const stale = STOCKS.map((quote) => {
      const row: Record<string, unknown> = { ...quote }
      delete row.volumeSession
      delete row.volumeDate
      return row
    })

    serve({ stocks: jsonResponse(200, stale) })
    render(<App />)

    await screen.findByText('NVIDIA Corp.')
    expect(bodyRows(stockTable())).toHaveLength(STOCKS.length)
  })

  it('renders an absent change as an absence rather than a flat day', async () => {
    serve()
    render(<App />)
    await screen.findByText('Reddit Inc.')

    // No previous close is nothing to measure from. `+$0.00` would claim
    // the price was unchanged.
    expect(cellText(rowFor(stockTable(), 'RDDT'), 3)).toContain('—')
    expect(cellText(rowFor(stockTable(), 'RDDT'), 4)).toContain('—')
  })

  it('sorts a fund with no market cap last in both directions', async () => {
    serve()
    render(<App />)
    await screen.findByText('SPDR S&P 500 ETF Trust')

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'marketCap' } })
    expect(cellText(bodyRows(stockTable())[bodyRows(stockTable()).length - 1], 0)).toBe('SPY')

    // Ascending too: coerced to zero, a fund would sort to the *top* and a
    // column of dollars would state that SPY is worth nothing.
    fireEvent.click(within(chainOrStockHeader('Market cap')).getByRole('button'))
    expect(cellText(bodyRows(stockTable())[bodyRows(stockTable()).length - 1], 0)).toBe('SPY')
  })
})

function chainOrStockHeader(label: string): HTMLElement {
  return within(stockTable())
    .getAllByRole('columnheader')
    .find((h) => h.textContent?.trim().startsWith(label)) as HTMLElement
}

describe('the chain is fetched for one underlying at a time', () => {
  it('asks for nothing until an underlying is chosen', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // `useChain(null)` stays disabled: three vendor operations hang off
    // this call, so a chain nobody asked about is not free.
    expect(chainCalls).toHaveLength(0)
    expect(await screen.findByText(/Pick an underlying to read its chain/)).toBeTruthy()
  })

  it('fetches the chosen chain and renders its ladder', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    await openChain('NVDA')
    await screen.findByText('NVDA260914C00210000')
    expect(chainCalls.some((u) => u.includes('/markets/chain/NVDA'))).toBe(true)
  })
})

describe('open interest, per decision 15', () => {
  it('prints a real zero and an absence differently', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    await openChain('NVDA')
    await screen.findByText('NVDA260914C00210000')

    const rows = bodyRows(chainTable())
    // Open interest of zero is a real fact: nobody holds the contract.
    expect(cellText(rows[1], 10).trim()).toBe('0')
    // Absent is a different fact, and never a blank cell that reads as zero.
    expect(cellText(rows[2], 10)).toContain('—')
    expect(cellText(rows[0], 10).trim()).toBe('663')
  })

  it('has removed the "highest open interest" screen', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    const rank = screen.getByLabelText('Rank chain by')
    const options = within(rank).getAllByRole('option').map((o) => o.textContent)
    // A ranking option that returns the list unsorted is worse than an
    // absent one.
    expect(options).not.toContain('Highest open interest')
    expect(options).toContain('Highest IV')
  })

  it('has stopped the OI header being a sort trigger', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    await openChain('NVDA')
    await screen.findByText('NVDA260914C00210000')

    const header = within(chainTable())
      .getAllByRole('columnheader')
      .find((h) => h.textContent?.trim().startsWith('OI')) as HTMLElement
    // Leaving it clickable would restore the removed ranking by another
    // route, over a column that is mostly null.
    expect(within(header).queryByRole('button')).toBeNull()
    expect(within(chainTable()).getByRole('button', { name: /Volume/ })).toBeTruthy()
  })
})

describe('a derived implied volatility is not a measured one', () => {
  it('marks the derived rows and says what the mark means', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    await openChain('NVDA')
    await screen.findByText('NVDA260914C00210000')

    // Decision 10: IV is derived locally wherever Alpaca's own solve
    // returned nothing, and a chain silently mixing the two is worse than
    // either alone.
    expect(cellText(bodyRows(chainTable())[0], 11)).toContain('*')
    expect(cellText(bodyRows(chainTable())[1], 11)).not.toContain('*')
    expect(screen.getByText(/derived locally from the contract/)).toBeTruthy()
  })
})

describe('a contract with no quote cannot be traded from the row', () => {
  it('omits the Trade control rather than offering one that cannot price', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    await openChain('NVDA')
    await screen.findByText('NVDA260914P00220000')

    const put = bodyRows(chainTable())[2]
    // Buying lifts the ask and selling hits the bid; with neither there is
    // nothing to price an order against.
    expect(within(put).queryByRole('button', { name: /Trade/ })).toBeNull()
    expect(cellText(put, 12)).toContain('No quote')

    const call = bodyRows(chainTable())[0]
    expect(within(call).getByRole('button', { name: /Trade/ })).toBeTruthy()
  })
})

describe('a book that is not configured', () => {
  it('says so without blanking a page that does not depend on it', async () => {
    serve({
      account: jsonResponse(409, {
        error: {
          code: 'account_unavailable',
          message:
            'Cash trading is unavailable: ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY are not set.',
        },
      }),
    })
    useUIStore.setState({ accountMode: 'cash' })
    render(<App />)

    expect(await screen.findByText(/ALPACA_LIVE_API_KEY/)).toBeTruthy()
    // Prices and chains are the same for both books, so they stay on screen.
    expect(await screen.findByText('NVIDIA Corp.')).toBeTruthy()
  })
})

/** Two directions, and they are different conditions rather than degrees of
 * one. TanStack sets `status: 'error'` on **any** failed fetch and leaves
 * the existing `data` in place, so once this table is polled at 400ms a
 * non-null `error` stops meaning "there is nothing to show": most of the
 * time it means "the last of many reads failed, and 180 good rows are still
 * on screen". The failure panel belongs to the first case only. */
describe('a request that fails', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('reports the stock universe failing as a system condition', async () => {
    serve({
      stocks: jsonResponse(503, {
        error: { code: 'upstream', message: 'The market data provider did not answer.' },
      }),
    })
    render(<App />)

    // `error`, not `bearish`: a failed fetch is not a losing position.
    // A 5xx is worth one more attempt (`shouldRetry`), so the error state
    // is a retry-delay away rather than immediate.
    const alert = await screen.findByRole('alert', {}, { timeout: 4_000 })
    expect(alert.textContent).toContain('The market data provider did not answer.')
    expect(alert.className).toContain('text-error')
    // A cold failure: no snapshot ever arrived, so there is no table to
    // keep and the panel is the whole answer.
    expect(within(section('Stocks & ETFs')).queryByRole('table')).toBeNull()
  })

  /** 429 rather than 503 on purpose, twice over: it is the failure a
   * 150/min poll against a 200/min bucket actually risks, and `shouldRetry`
   * does not retry a 4xx, so the error state lands on the failed poll
   * itself rather than a retry delay later. */
  function rateLimited(): Response {
    return jsonResponse(429, {
      error: { code: 'rate_limited', message: 'Too many requests to the market data provider.' },
    })
  }

  it('keeps the table when a poll fails on top of a good snapshot', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let failing = false
    serve({ stocks: () => (failing ? rateLimited() : jsonResponse(200, STOCKS)) })
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    failing = true
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MARKETS_FOREGROUND_POLL_MS * 2)
    })

    // Every row still there: the prices are 400ms old, not absent. Blanking
    // them for one bad poll and restoring them on the next would strobe the
    // page two or three times a second under intermittent failure, and say
    // the feed is down while it is not.
    expect(bodyRows(stockTable())).toHaveLength(STOCKS.length)
    expect(screen.queryByRole('alert')).toBeNull()
    // The header carries it instead, beside the `Read …` stamp that has
    // stopped advancing because `dataUpdatedAt` only moves on success.
    expect(screen.getByText('Last poll failed')).toBeTruthy()

    // And it clears on the next good read — a staleness signal that stuck
    // would be a worse lie than none.
    failing = false
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MARKETS_FOREGROUND_POLL_MS * 2)
    })
    expect(screen.queryByText('Last poll failed')).toBeNull()
  })

  it('renders the snapshot a background poll failed after, not a panel', async () => {
    // The state a user reaches by sitting on Settings while the 5s
    // background leg fails once, then navigating to Markets: the cache
    // holds a good snapshot *and* an error, and before this was split the
    // arrival rendered a failure panel caused by a fetch that happened on a
    // page they were not looking at — inverting the reason the background
    // leg exists at all. Primed directly rather than by driving the router,
    // because what is under test is what Markets does on mount.
    let failing = false
    serve({ stocks: () => (failing ? rateLimited() : jsonResponse(200, STOCKS)) })
    await refetchStocks(queryClient)
    failing = true
    await refetchStocks(queryClient)

    render(<App />)

    expect(await screen.findByText('NVIDIA Corp.')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(await screen.findByText('Last poll failed')).toBeTruthy()
  })
})

/** Decision 18's foreground state is wired by *this page* mounting the
 * cadence hook — which page is open is read from the router rather than
 * from a store flag, so the mount is the wiring and this is the test that
 * fails if it is removed. The cadence itself is pinned in
 * `useMarketPoll.test.tsx`; what is asserted here is only that the page
 * mounts it rather than sitting on the 5s background interval. */
describe('the foreground cadence', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('re-reads the stock table while Markets is open', async () => {
    // `shouldAdvanceTime` so the interval is a fake timer from the start
    // while RTL's real-time waits still resolve.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const fetchMock = serve()
    const stocksReads = () =>
      fetchMock.mock.calls.filter((c) => String(c[0]).includes('/markets/stocks')).length

    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    const before = stocksReads()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(MARKETS_FOREGROUND_POLL_MS * 3)
    })

    // Three intervals, three reads. Greater-or-equal rather than exact
    // because `shouldAdvanceTime` also moves the clock by whatever real
    // time the awaits above took; the background rate would produce none
    // of them in 1.2s, which is the distinction under test.
    expect(stocksReads() - before).toBeGreaterThanOrEqual(3)
  })
})

/** Decision 18's rule 4 at the page level. The merge is `quotes.ts`'s and is
 * tested there; what this pins is that the table reads *through* it — that
 * a pushed price reaches the screen and the change beside it is recomputed
 * from that price rather than read off the poll's response, which is how a
 * row ends up reporting +1.2% beside a price that is down. */
describe('the stock table renders the merged quote, not the wire row', () => {
  it('shows a streamed price and a change derived from it', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // A push, stamped later than the poll's observation, so rule 2 takes it.
    act(() => {
      useUIStore.getState().applyStreamedQuote('NVDA', 220, '2026-08-07T19:46:00Z')
    })

    const row = rowFor(stockTable(), 'NVDA')
    // The basis is the poll's `previousClose`, 218.37, and it does not move
    // when a price is pushed over it: 220.00 − 218.37 = +1.63, +0.75%. Read
    // off the poll's own price it would still say −$0.20, beside a price
    // that is up — which is the disagreement rule 4 exists to prevent.
    expect(cellText(row, 2)).toBe('$220.00')
    expect(cellText(row, 3)).toBe('+$1.63')
    expect(cellText(row, 4)).toBe('+0.75%')
  })

  /** The served row has no previous daily bar. A derived change must stay
   * absent rather than become a flat day the moment a price is pushed over
   * it — a `+$0.00` in a column of dollars claims the price is unchanged. */
  it('keeps an unmeasurable change unmeasurable under a streamed price', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    act(() => {
      useUIStore.getState().applyStreamedQuote('RDDT', 210, '2026-08-07T19:46:00Z')
    })

    const row = rowFor(stockTable(), 'RDDT')
    expect(cellText(row, 2)).toBe('$210.00')
    expect(cellText(row, 3)).toContain('—')
  })
})


/** **Step 15 (b), decision 18's viewport hint.** Which rows are on screen,
 * debounced until the viewport settles, diffed, and sent only on a real
 * difference.
 *
 * What is pinned here is the wiring: the observer watches the *stock*
 * table's rows and nothing else, the debounce is trailing-edge, an
 * unchanged set is silent, leaving the page says so, and a refusal is not
 * fatal. The payload rules themselves — upper case, the 16-character
 * ticker shape, the OCC refusal, the 64 cap, the diff — are pure and
 * pinned in `markets.test.ts`; the cap in particular cannot be reached
 * from here, because a page renders at most `PAGE_SIZE` rows.
 *
 * jsdom implements no layout and so ships no `IntersectionObserver`. The
 * suite-wide stub in `test/setup.ts` is inert; this one is driveable, so a
 * test can say which rows are on screen. */
type Observation = {
  callback: IntersectionObserverCallback
  targets: Set<Element>
}

let observations: Observation[] = []

class DriveableIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null
  readonly rootMargin: string = '0px'
  // Recent lib.dom, and required on the interface.
  readonly scrollMargin: string = '0px'
  readonly thresholds: readonly number[] = [0]
  private readonly own: Observation

  constructor(callback: IntersectionObserverCallback) {
    this.own = { callback, targets: new Set() }
    observations.push(this.own)
  }

  observe(target: Element): void {
    this.own.targets.add(target)
  }

  unobserve(target: Element): void {
    this.own.targets.delete(target)
  }

  disconnect(): void {
    this.own.targets.clear()
    observations = observations.filter((o) => o !== this.own)
  }

  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

/** Report a set of rows as on screen, and everything else as off it. */
function onScreen(symbols: string[]): void {
  const observation = observations.at(-1)
  if (observation === undefined) throw new Error('nothing observed the stock table')
  const entries = [...observation.targets].map((target) => ({
    target,
    isIntersecting: symbols.includes((target as HTMLElement).dataset.symbol ?? ''),
  }))
  act(() => {
    observation.callback(
      entries as unknown as IntersectionObserverEntry[],
      null as unknown as IntersectionObserver,
    )
  })
}

/** The symbols the observer is watching, in DOM order — which is the order
 * the hint has to be in, since order decides who survives both caps. */
function observedSymbols(): string[] {
  const observation = observations.at(-1)
  if (observation === undefined) throw new Error('nothing observed the stock table')
  return [...observation.targets].map((t) => (t as HTMLElement).dataset.symbol ?? '')
}

async function settleViewport(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(MARKETS_VIEWPORT_DEBOUNCE_MS)
  })
}

/** Twenty rows, which is two pages at `PAGE_SIZE` 15 — the only way to
 * reach a page turn, since the four-row fixture above never paginates.
 * Volume descends with the index, so under the default `active` rank the
 * DOM order is the fixture order and page 1 is A–O. */
const PAGED_STOCKS: StockQuote[] = Array.from({ length: 20 }, (_, i) => {
  const letter = String.fromCharCode(65 + i)
  return {
    symbol: `PG${letter}`,
    name: `Paged Holdings ${letter}`,
    price: 100 + i,
    at: '2026-08-07T19:45:00Z',
    previousClose: 100,
    volume: 20_000_000 - i * 100_000,
    volumeSession: 'completed',
    volumeDate: '2026-09-11',
    avgVolume: 10_000_000,
    marketCap: 10 + i,
  }
})

describe('the viewport hint', () => {
  beforeEach(() => {
    observations = []
    sendMarketsVisible.mockClear()
    sendMarketsVisible.mockReturnValue(true)
    vi.stubGlobal('IntersectionObserver', DriveableIntersectionObserver)
    // `shouldAdvanceTime` so the debounce is a fake timer from the start
    // while RTL's real-time waits still resolve — the same arrangement the
    // foreground-cadence test above uses.
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('names the rows on screen once the viewport settles', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // Mounting alone says nothing: no hint is in force, nothing is
    // reported on screen, and an empty list against a server that holds
    // none is not news.
    await settleViewport()
    expect(sendMarketsVisible).not.toHaveBeenCalled()

    const order = observedSymbols()
    onScreen(['NVDA', 'SPY'])
    await settleViewport()

    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
    expect(sendMarketsVisible).toHaveBeenCalledWith(
      order.filter((s) => s === 'NVDA' || s === 'SPY'),
    )
  })

  it('sends once for a scroll, not once per frame', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // Three changes inside one window. A resubscribe is a gap in the
    // marks, so a scroll crossing forty rows has to cost one message.
    onScreen(['NVDA'])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MARKETS_VIEWPORT_DEBOUNCE_MS / 4)
    })
    onScreen(['NVDA', 'SPY'])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(MARKETS_VIEWPORT_DEBOUNCE_MS / 4)
    })
    onScreen(['SPY', 'RDDT'])
    expect(sendMarketsVisible).not.toHaveBeenCalled()

    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
    expect(sendMarketsVisible).toHaveBeenCalledWith(['SPY', 'RDDT'])
  })

  it('says nothing when the set has not changed', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    onScreen(['NVDA', 'SPY'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

    // The same rows reported again — a re-render, a poll, a scroll that
    // moved nothing over an edge. The server would answer UNCHANGED; the
    // cheaper answer is not to ask.
    onScreen(['NVDA', 'SPY'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
  })

  it('sends an empty list on the way off the page', async () => {
    serve()
    const { unmount } = render(<App />)
    await screen.findByText('NVIDIA Corp.')

    onScreen(['NVDA'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

    unmount()
    // Navigated away: nothing of this table is on screen, and an empty
    // list is the correct way to say so rather than leaving the engine
    // spending slots on rows nobody is looking at.
    expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
    expect(sendMarketsVisible).toHaveBeenLastCalledWith([])
  })

  it('does not record a hint the socket could not take', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    // `false` means there was no open socket to say it on. Not delivered,
    // so it is not remembered as sent — and the next genuine settle says
    // it again rather than believing a hint is in force that never left
    // the browser.
    sendMarketsVisible.mockReturnValue(false)
    onScreen(['NVDA'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

    sendMarketsVisible.mockReturnValue(true)
    onScreen(['NVDA'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
    expect(sendMarketsVisible).toHaveBeenLastCalledWith(['NVDA'])
  })

  it('never names a contract, and never watches the chain table', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')
    await openChain('NVDA')

    // The chain's rows are OCC contracts and are not this message: the
    // engine refuses one by name, and the refusal is of the whole hint.
    // Structurally impossible here, because only the stock table's
    // `tbody` is observed — which is the letter of the spec's "the
    // visible rows". Whether the chain's *underlying* should be hinted
    // while its chain is open is an open question recorded in the spec.
    expect(observedSymbols()).toEqual(expect.arrayContaining(['NVDA', 'SPY']))
    expect(observedSymbols().some((s) => s.length > 6)).toBe(false)

    onScreen(observedSymbols())
    await settleViewport()
    const hint = sendMarketsVisible.mock.calls.at(-1)?.[0] ?? []
    expect(hint).not.toContain('NVDA260914C00210000')
    expect(hint).toContain('NVDA')
  })

  it('drops a row the engine would refuse rather than losing the message', async () => {
    // A served row whose symbol is wider than the engine's 16-character
    // equity shape — the band this endpoint's transport filter admits for
    // `subscribe`'s sake. The message is applied whole or not at all, so
    // sending it would cost the hint entirely; the row is filtered out.
    serve({
      stocks: jsonResponse(200, [
        ...STOCKS,
        {
          ...STOCKS[0],
          symbol: 'ABCDEFGHIJKLMNOPQ',
          name: 'Seventeen Characters Ltd.',
          volume: 1_000,
        },
      ]),
    })
    render(<App />)
    await screen.findByText('Seventeen Characters Ltd.')

    onScreen(observedSymbols())
    await settleViewport()

    const hint = sendMarketsVisible.mock.calls.at(-1)?.[0] ?? []
    expect(hint).not.toContain('ABCDEFGHIJKLMNOPQ')
    expect(hint).toContain('NVDA')
  })

  it('treats a refused hint as no longer in force, silently and without retrying', async () => {
    serve()
    render(<App />)
    await screen.findByText('NVIDIA Corp.')

    onScreen(['NVDA', 'SPY'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

    act(() => {
      useUIStore.getState().recordStreamError({
        code: 'subscription_refused',
        message: 'The engine refused the viewport hint.',
      })
    })
    await settleViewport()

    // **Finding F5.** No retry: a refusal is one refused message on a live
    // socket, the previous hint stands, and the page is polled regardless.
    expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
    // And nothing on the page says the feed failed. A refused
    // subscription hint is not a failed market-data feed, and every row
    // is still rendering its polled price.
    expect(screen.queryByText('Last poll failed')).not.toBeInTheDocument()
    expect(screen.queryByText(/refused/i)).not.toBeInTheDocument()
    expect(rowFor(stockTable(), 'NVDA')).toBeInTheDocument()

    // What the refusal *did* change: the hint is no longer believed to be
    // in force, so the next genuine settle sends it again instead of
    // diffing it away and leaving the tier empty for the session.
    onScreen(['NVDA', 'SPY'])
    await settleViewport()
    expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
    expect(sendMarketsVisible).toHaveBeenLastCalledWith(['NVDA', 'SPY'])
  })

  /** **Finding WEB-7.** "Never `[]` between two pages of the same table"
   * used to hold only because the rebuilt observer's first delivery beat
   * the debounce timer. The three tests below drive the *other*
   * interleaving — the debounce fires first, which is what a throttled
   * tab or a long main-thread block produces — and the third one pins the
   * case the gate must **not** swallow. */
  describe('rebuilding the observer', () => {
    it('never empties the hint between two pages of the same table', async () => {
      serve({ stocks: jsonResponse(200, PAGED_STOCKS) })
      render(<App />)
      await screen.findByText('Paged Holdings A')

      const firstPage = observedSymbols()
      expect(firstPage).toHaveLength(15)
      onScreen(firstPage.slice(0, 3))
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
      expect(sendMarketsVisible).toHaveBeenLastCalledWith(firstPage.slice(0, 3))

      fireEvent.click(within(section('Stocks & ETFs')).getByRole('button', { name: 'Next' }))
      await screen.findByText('Paged Holdings P')

      // The whole debounce window elapses with the new observer still
      // silent. Nothing may go out: the viewport never emptied, so an
      // empty hint here is a resubscribe of rows that are on screen.
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

      // The observer answers late, and *that* is what sends — one
      // message, the new page, never an `[]` in front of it.
      const secondPage = observedSymbols()
      expect(secondPage).not.toEqual(firstPage)
      onScreen(secondPage.slice(0, 2))
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
      expect(sendMarketsVisible).toHaveBeenLastCalledWith(secondPage.slice(0, 2))
      expect(sendMarketsVisible.mock.calls.every(([hint]) => hint.length > 0)).toBe(true)
    })

    it('never empties the hint on a re-sort of the same rows', async () => {
      serve()
      render(<App />)
      await screen.findByText('NVIDIA Corp.')

      const byVolume = observedSymbols()
      onScreen(byVolume)
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(1)
      expect(sendMarketsVisible).toHaveBeenLastCalledWith(byVolume)

      // A re-sort renders the same rows in a different order, which
      // rebuilds the observer exactly as a page turn does. Same hazard.
      fireEvent.change(within(section('Stocks & ETFs')).getByLabelText('Rank stocks by'), {
        target: { value: 'gainers' },
      })
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

      const byGain = observedSymbols()
      // Same set, different order — and the diff is ordered, mirroring the
      // server's tuple comparison, so this is news on its own.
      expect(byGain).not.toEqual(byVolume)
      expect([...byGain].sort()).toEqual([...byVolume].sort())
      onScreen(byGain)
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
      expect(sendMarketsVisible).toHaveBeenLastCalledWith(byGain)
    })

    it('still empties the hint when the rows themselves go away', async () => {
      serve()
      render(<App />)
      await screen.findByText('NVIDIA Corp.')

      onScreen(['NVDA', 'SPY'])
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(1)

      // A search that matches nothing. There is no observer to wait on
      // here and no answer coming, so an empty `visible` set means
      // *nothing on screen* rather than *not yet known* — and the engine
      // must stop holding slots for rows that are gone. The guard above
      // is about an unanswered observer, never about suppressing `[]`.
      fireEvent.change(
        within(section('Stocks & ETFs')).getByLabelText('Search stocks by symbol or name'),
        { target: { value: 'no such company' } },
      )
      await settleViewport()
      expect(sendMarketsVisible).toHaveBeenCalledTimes(2)
      expect(sendMarketsVisible).toHaveBeenLastCalledWith([])
    })
  })
})
