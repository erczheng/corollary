import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import type { OptionContract, Position, RiskLimit, StockQuote } from '../lib/types'

const initialState = useUIStore.getState()

/** Both tickets read the ceiling from the **server** now.
 *
 * Settings stopped writing the Zustand slice when it migrated: it writes
 * `PATCH /api/settings/limits` and the audit log records it. A ticket still
 * reading the store would agree at rest and diverge the moment a ceiling was
 * edited — Settings and the audit log showing the new number while the
 * ticket quoted the old one. So every test here states the ceiling by
 * *serving* it, which is the only place it now comes from.
 */
const POSITION: Position = {
  id: 'AAPL261218C00340000',
  symbol: 'AAPL',
  contract: '$340 Call Dec 18',
  last: 16.139,
  underlying: 332.55,
  costBasis: 1255.0,
  value: 1595.0,
  quantity: 1,
  pnl: 340.0,
  pnlPct: 27.0916,
  bid: 16.059,
  ask: 16.219,
  direction: 'long',
  legs: [{ symbol: 'AAPL261218C00340000', strike: 340.0, right: 'call', side: 'long', ratio: 1 }],
  expiry: '2026-12-18',
  strategyId: null,
  openedByStrategyId: null,
  managedExit: null,
  attachedExit: null,
  valueHistory: [{ date: '2026-09-11', value: 1635.0 }],
}

/** One quoted name and one tradeable contract, which is all the chain half
 * of this file needs. The contract is priced so that a single lot is 1.99%
 * of the served equity — under a 7% ceiling and over a 1% one. */
const STOCKS: StockQuote[] = [
  {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    price: 332.55,
    at: '2026-08-07T19:45:00Z',
    previousClose: 331.35,
    volume: 50_000_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-11',
    avgVolume: 52_000_000,
    marketCap: 3_540,
  },
]

const CHAIN: OptionContract[] = [
  {
    symbol: 'AAPL261218C00340000',
    strike: 340,
    expiration: '2026-12-18',
    type: 'call',
    last: 19.9,
    previousClose: 19.2,
    change: 0.7,
    changePct: 3.65,
    bid: 19.8,
    ask: 20.0,
    volume: 1_200,
    openInterest: 4_400,
    iv: 0.31,
  },
]

const EQUITY = 99_901.08

function limits(value: number | null): RiskLimit[] {
  return [
    {
      key: 'max_risk_per_trade_pct',
      label: 'Max risk per trade',
      value,
      unit: '%',
      min: 1,
      max: 25,
      help: 'Ceiling on what one position may lose, as a share of account equity.',
    } as RiskLimit,
  ]
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

const NO_STATS = {
  avgWin: null,
  avgWinPct: null,
  avgLoss: null,
  avgLossPct: null,
  lifetimePnl: 0,
  wins: 0,
  losses: 0,
  notBooked: 0,
  notBookedSymbols: [],
}

/** `served` is the risk-limit payload under test: a list with a value, a
 * list with a null value, or an empty list. All three are real answers from
 * `GET /api/settings/limits`, and the tickets have to tell them apart. */
function stubFetch(served: RiskLimit[]): void {
  vi.stubGlobal(
    'fetch',
    vi.fn((input: unknown) => {
      const url = String(input)
      if (url.includes('/settings/limits')) return Promise.resolve(jsonResponse(200, served))
      if (url.includes('/markets/stocks')) return Promise.resolve(jsonResponse(200, STOCKS))
      if (url.includes('/markets/chain/')) return Promise.resolve(jsonResponse(200, CHAIN))
      if (url.includes('/markets/underlyings')) return Promise.resolve(jsonResponse(200, []))
      if (url.includes('/positions/working')) return Promise.resolve(jsonResponse(200, []))
      if (url.includes('/positions')) return Promise.resolve(jsonResponse(200, [POSITION]))
      if (url.includes('/activity/stats')) return Promise.resolve(jsonResponse(200, NO_STATS))
      if (url.includes('/activity')) {
        return Promise.resolve(
          jsonResponse(200, { items: [], total: 0, page: 0, pageSize: 15, hasMore: false }),
        )
      }
      return Promise.resolve(jsonResponse(200, { equity: EQUITY }))
    }),
  )
}

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState, lastTickAt: new Date().toISOString() }, true)
  queryClient.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

/** The per-trade risk ceiling is quoted in two different tickets, and it is
 * editable in Settings. These tests exist because of how the two used to
 * read it.
 *
 * `OrderTicket` did `RISK_LIMITS.find(…)?.value ?? 0` *inside* the component,
 * so it was merely stale. `ChainOrderTicket` did the same lookup with `?? 7`
 * at **module scope** — evaluated once when the module was first imported,
 * which means an edited limit could never reach it again for the life of the
 * tab, through any number of remounts. The two fallbacks disagreed, so one
 * invented a ceiling nobody had set and the other reported every trade as
 * over-limit.
 *
 * None of this enforces anything. CLAUDE.md rule 4: the engine enforces and
 * never trusts a limit that arrived from the client. These are advisory
 * readouts, and being advisory is not a licence to be wrong. */
describe('the per-trade risk ceiling follows the server', () => {
  async function openAddTicket(served: RiskLimit[]): Promise<void> {
    stubFetch(served)
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Activity' }))

    // Wait for the *position* to arrive, not merely for some table to
    // exist — the ledger's header row is in the document immediately, so
    // findAllByRole('row') resolves before any position has been fetched.
    const row = (await screen.findByText(POSITION.contract)).closest('tr')!

    fireEvent.click(within(row).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to position' }))
  }

  it('quotes the served ceiling in the position ticket', async () => {
    await openAddTicket(limits(7))
    expect(await screen.findByText(/against a 7% per-trade ceiling/)).toBeInTheDocument()
  })

  it('quotes an edited ceiling in the position ticket', async () => {
    await openAddTicket(limits(12))

    expect(await screen.findByText(/against a 12% per-trade ceiling/)).toBeInTheDocument()
    expect(screen.queryByText(/against a 7% per-trade ceiling/)).not.toBeInTheDocument()
  })

  /** With no ceiling configured the ticket says so rather than printing a
   * number. The old `?? 0` reported a 0% ceiling, which describes an account
   * that may not trade at all — a claim nobody made. `riskLimitFor` returns
   * `number | null` and the null has to survive to the screen. */
  it('says no ceiling is configured rather than inventing one', async () => {
    await openAddTicket(limits(null))

    expect(await screen.findByText(/No per-trade ceiling is configured/)).toBeInTheDocument()
  })

  async function openChainTicket(served: RiskLimit[]): Promise<void> {
    stubFetch(served)
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Markets' }))

    // The chain is fetched one underlying at a time and stays disabled until
    // one is chosen, so the ticket is two steps in now rather than one.
    const box = within(await screen.findByRole('region', { name: 'Options chains' })).getByLabelText(
      'Search underlying',
    )
    fireEvent.focus(box)
    fireEvent.change(box, { target: { value: 'AAPL' } })
    fireEvent.mouseDown(await screen.findByRole('option', { name: 'AAPL' }))

    fireEvent.click(await screen.findByRole('button', { name: /^Trade AAPL261218C00340000/ }))
  }

  /** The module-scope case. A 1% ceiling puts a single contract over it, so
   * the advisory warning has to appear *and* name the served figure. */
  it('quotes the served ceiling in the chain ticket, which used to capture it at import', async () => {
    await openChainTicket(limits(1))

    const warning = await screen.findByText(
      /per-trade ceiling\. The engine enforces this limit/,
    )
    expect(warning.textContent).toContain('1% per-trade ceiling')
    expect(warning.textContent).not.toContain('7% per-trade ceiling')
  })

  it('raises no over-limit warning in the chain ticket when no ceiling is configured', async () => {
    await openChainTicket(limits(null))

    // And says which of the two it is, rather than going quiet: an absent
    // ceiling and a satisfied one look identical if neither says anything.
    expect(await screen.findByText(/No per-trade risk ceiling is configured/)).toBeInTheDocument()
    expect(
      screen.queryByText(/per-trade ceiling\. The engine enforces this limit/),
    ).not.toBeInTheDocument()
  })
})
