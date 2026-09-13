import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import type { AccountResponse, OptionContract, RiskLimit, StockQuote } from '../lib/types'

/** Markets reads three market endpoints and the account, and writes none of
 * them. Every payload below is the shape
 * `http://127.0.0.1:5173/api/markets/*` actually answers with — including
 * the nulls, which are the point: a live chain has no bid on roughly half
 * its contracts, a fund has no market capitalisation, and open interest is
 * absent wherever the vendor did not carry it (decision 15).
 */
const STOCKS: StockQuote[] = [
  {
    symbol: 'NVDA',
    name: 'NVIDIA Corp.',
    price: 218.17,
    change: -0.2,
    changePct: -0.09,
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
    change: 6.415,
    changePct: 0.85,
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
    // No prior daily bar: there is no move to state.
    change: null,
    changePct: null,
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
    change: 1,
    changePct: 2,
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
  stocks?: Response
  chain?: Response
  account?: Response
}

let chainCalls: string[] = []

function serve(overrides: Overrides = {}) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)

    if (url.includes('/markets/stocks')) {
      return Promise.resolve(overrides.stocks ?? jsonResponse(200, STOCKS))
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

describe('a request that fails', () => {
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
  })
})
