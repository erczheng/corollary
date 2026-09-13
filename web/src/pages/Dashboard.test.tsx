import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { equityCurve, oldestRead, realizedWinRate } from './Dashboard'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { formatUsd } from '../lib/format'
import type {
  AccountResponse,
  ActivityItem,
  ActivityStats,
  EngineStateResponse,
  EquityCurvePoint,
  Page,
  PortfolioHistoryResponse,
  Position,
} from '../lib/types'

/** The Dashboard reads six endpoints and holds no server state of its own.
 * These payloads are the shapes the live paper account returned on
 * 2026-09-12 — including the 223 leading zero-equity points a one-year
 * history window pads itself back with, and `haltedReason: null`, which is
 * what a cold-start halt actually carries.
 *
 * Nothing here comes from `mockData.ts`. The page is a data-source swap, so
 * a fixture imported from the Phase 1 file would prove the old rendering
 * against the old data and nothing about the new path. */

const PAPER = {
  account: 'paper',
  status: 'ACTIVE',
  currency: 'USD',
  cash: 53386.08,
  equity: 99901.08,
  lastEquity: 100116.08,
  dayChange: -215.0,
  balanceTrend: { changePct: -0.2148, comparedTo: 'vs previous close' },
  buyingPower: 319786.72,
  optionsBuyingPower: 71215.08,
  longMarketValue: 54186.0,
  shortMarketValue: -7671.0,
  netPositionValue: 46515.0,
  grossPositionValue: 61857.0,
  derivedEquity: 99901.08,
  equityReconciles: true,
  equityDifference: 0,
  margin: {
    multiplier: 4,
    marginClass: 'pdt',
    label: 'Pattern day-trader margin account',
    note: 'The broker reports a multiplier of 4.',
  },
  optionsApprovedLevel: 3,
  optionsTradingLevel: 3,
  tradingBlocked: false,
  accountBlocked: false,
  transfersBlocked: false,
  cashAccountAvailable: false,
  cashAccountUnavailableReason:
    'Cash trading is unavailable: ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY are not set.',
  missingLiveCredentialEnvVars: ['ALPACA_LIVE_API_KEY', 'ALPACA_LIVE_SECRET_KEY'],
} as unknown as AccountResponse

/** Three zero-equity points before the account was funded, then four real
 * closes. The live one-year window opens with 223 of those zeroes. */
const HISTORY: PortfolioHistoryResponse = {
  account: 'paper',
  period: '1A',
  timeframe: '1D',
  baseValue: 100000.0,
  baseValueAsof: '2026-08-03',
  points: [
    { at: '2026-07-30T00:00:00Z', equity: 0.0, profitLoss: 0.0, profitLossPct: 0.0 },
    { at: '2026-07-31T00:00:00Z', equity: 0.0, profitLoss: 0.0, profitLossPct: 0.0 },
    { at: '2026-08-03T00:00:00Z', equity: 0.0, profitLoss: 0.0, profitLossPct: 0.0 },
    { at: '2026-08-04T00:00:00Z', equity: 100000.0, profitLoss: 0.0, profitLossPct: 0.0 },
    { at: '2026-08-05T00:00:00Z', equity: 100240.0, profitLoss: 240.0, profitLossPct: 0.0024 },
    { at: '2026-09-11T00:00:00Z', equity: 100116.08, profitLoss: 277.0, profitLossPct: 0.0028 },
    { at: '2026-09-12T00:00:00Z', equity: 99901.08, profitLoss: -215.0, profitLossPct: -0.0021 },
  ],
  // After the curve starts: most of this window predates Corollary.
  t0: '2026-09-12T19:14:37.242679Z',
  pointsBeforeT0: 252,
}

/** Folded server-side over every realized trade (spec decision 11). Two and
 * two, so the win rate is a number rather than 0 or 100 and a sign error
 * cannot pass. */
const STATS: ActivityStats = {
  avgWin: 412.5,
  avgWinPct: 33.25,
  avgLoss: -29.0,
  avgLossPct: -27.1616,
  lifetimePnl: 767.0,
  wins: 2,
  losses: 2,
  notBooked: 0,
  notBookedSymbols: [],
}

const NOTHING_REALIZED: ActivityStats = {
  ...STATS,
  avgWin: null,
  avgWinPct: null,
  avgLoss: null,
  avgLossPct: null,
  lifetimePnl: 0,
  wins: 0,
  losses: 0,
}

const HALTED: EngineStateResponse = {
  halted: true,
  haltedReason: null,
  haltedAt: null,
  t0: '2026-09-12T19:14:37.242679Z',
}

const HALTED_WITH_REASON: EngineStateResponse = {
  halted: true,
  haltedReason: 'The Alpaca stream closed and did not come back within 90 seconds.',
  haltedAt: '2026-09-12T14:30:00.000000Z',
  t0: '2026-09-12T19:14:37.242679Z',
}

const RUNNING: EngineStateResponse = { ...HALTED, halted: false }

const FILL_WIN: ActivityItem = {
  id: '20260910131125217::a9d576c2',
  time: '2026-09-11T17:11:25.217000Z',
  contract: 'AAPL $340 Call Dec 18',
  action: 'BTO',
  price: 12.55,
  quantity: 1,
  pnl: null,
  pnlPct: null,
  amount: null,
  status: 'filled',
  rejectionReason: null,
}

const FILL_LOSS: ActivityItem = {
  id: '20260911000000000::2fd1270e',
  time: '2026-09-12T01:15:24.157807Z',
  contract: 'NVDA $240 Call Sep 11',
  action: 'STC',
  price: 0.0,
  quantity: 1,
  pnl: -2.0,
  pnlPct: -100.0,
  amount: null,
  status: 'filled',
  rejectionReason: null,
}

const REJECTED: ActivityItem = {
  id: '20260910131100000::b1c2d3e4',
  time: '2026-09-11T16:02:00.000000Z',
  contract: 'TSLA $500 Call Oct 16',
  action: 'BTO',
  price: null,
  quantity: 4,
  pnl: null,
  pnlPct: null,
  amount: null,
  status: 'rejected',
  rejectionReason: 'max_risk_per_trade_pct — 9.4% of equity against a 7% ceiling',
}

const DEPOSIT: ActivityItem = {
  id: '20260805000000000::16d48149',
  time: '2026-08-05T03:48:40.594917Z',
  contract: '—',
  action: 'DEPOSIT',
  price: null,
  quantity: null,
  pnl: null,
  pnlPct: null,
  amount: 25000,
  status: 'filled',
  rejectionReason: null,
}

const WITHDRAWAL: ActivityItem = {
  ...DEPOSIT,
  id: '20260806000000000::27e59250',
  time: '2026-08-06T03:48:40.594917Z',
  action: 'WITHDRAWAL',
  amount: -1500,
}

const LEDGER = [FILL_LOSS, FILL_WIN, REJECTED, DEPOSIT, WITHDRAWAL]

/** The page reads the *count* and nothing else — it is the number quoted in
 * the reason Flatten is disabled. Shaped rather than fully built so this
 * suite does not have to track every field on `Position`. */
const POSITIONS = [{ id: 'one' }, { id: 'two' }] as unknown as Position[]

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

/** The 409 `?account=cash` gets while the live keys are absent. */
const UNAVAILABLE = jsonResponse(409, {
  error: {
    code: 'account_unavailable',
    message:
      'The cash account is not configured: ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY are ' +
      'both required and neither is set.',
  },
})

/** A promise that never settles — the loading state, held open. */
function pending(): Response {
  return new Promise<never>(() => {}) as unknown as Response
}

function pageOf(items: ActivityItem[]): Page<ActivityItem> {
  return { items, total: items.length, page: 0, pageSize: 30, hasMore: false }
}

interface Answers {
  account?: Response
  history?: PortfolioHistoryResponse | Response
  stats?: ActivityStats | Response
  engine?: EngineStateResponse | Response
  ledger?: ActivityItem[] | Response
  positions?: Position[] | Response
  /** Called with the parsed query of every `/api/activity` request, so a
   * test can assert what went *out* — the status filter is the server's
   * job now, not this page's. */
  onActivity?: (params: URLSearchParams) => void
  /** Called with the parsed query of every `/api/account/history` request. */
  onHistory?: (params: URLSearchParams) => void
  /** Called with the POST body of a halt or resume. */
  onEngineWrite?: (path: string, body: unknown) => void
}

function isResponse(value: unknown): value is Response {
  return typeof value === 'object' && value !== null && 'json' in value
}

/** Answer by URL. Order matters — `/account/history` is checked before
 * `/account`, `/activity/stats` before `/activity`, and the two engine
 * writes before `/engine/state`. */
function stubFetch(answers: Answers = {}) {
  let engineState = answers.engine ?? HALTED

  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = String(input)
    const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))
    const cash = params.get('account') === 'cash'

    if (url.includes('/account/history')) {
      answers.onHistory?.(params)
      if (cash) return Promise.resolve(answers.account ?? UNAVAILABLE)
      const history = answers.history ?? HISTORY
      return Promise.resolve(isResponse(history) ? history : jsonResponse(200, history))
    }
    if (url.includes('/activity/stats')) {
      const stats = answers.stats ?? STATS
      return Promise.resolve(isResponse(stats) ? stats : jsonResponse(200, stats))
    }
    if (url.includes('/activity')) {
      answers.onActivity?.(params)
      const ledger = answers.ledger ?? LEDGER
      return Promise.resolve(isResponse(ledger) ? ledger : jsonResponse(200, pageOf(ledger)))
    }
    if (url.includes('/positions')) {
      const positions = answers.positions ?? POSITIONS
      return Promise.resolve(isResponse(positions) ? positions : jsonResponse(200, positions))
    }
    if (url.includes('/engine/halt') || url.includes('/engine/resume')) {
      const body: unknown = init?.body === undefined ? undefined : JSON.parse(String(init.body))
      answers.onEngineWrite?.(url.includes('/engine/halt') ? 'halt' : 'resume', body)
      engineState = url.includes('/engine/halt')
        ? { ...HALTED, halted: true, haltedAt: '2026-09-12T18:00:00.000000Z' }
        : RUNNING
      return Promise.resolve(jsonResponse(200, engineState))
    }
    if (url.includes('/engine/state')) {
      return Promise.resolve(isResponse(engineState) ? engineState : jsonResponse(200, engineState))
    }
    if (url.includes('/account')) {
      if (cash) return Promise.resolve(answers.account ?? UNAVAILABLE)
      return Promise.resolve(jsonResponse(200, PAPER))
    }
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location, and these tests navigate.
  window.history.pushState({}, '', '/')
  // Rule 5: every start is Paper. A leaked `cash` here would be a test
  // ordering bug that reads as a data bug.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

/** Escape a contract label for use inside a regex — strikes carry a `$`. */
function escaped(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function section(heading: string): HTMLElement {
  return screen.getByRole('heading', { name: heading }).closest('section')!
}

/** Render and wait for the balance to land. Every figure on the page is
 * server state now, so there is nothing to assert before it does. */
async function renderDashboard() {
  render(<App />)
  await screen.findByText(formatUsd(PAPER.equity))
}

/* -------------------------------------------------------------------------
 * The page's arithmetic, without rendering anything
 * ---------------------------------------------------------------------- */

describe('equityCurve', () => {
  it('drops the zero pad before the account was funded', () => {
    const series = equityCurve(HISTORY.points)

    expect(series).toHaveLength(4)
    expect(series[0]).toEqual({ date: '2026-08-04', value: 100000 })
  })

  it('keeps a zero that appears after the curve has started', () => {
    // Not padding: an account that reached zero equity mid-curve did so for
    // real, and hiding it would hide the worst day in the book.
    const wiped: EquityCurvePoint[] = [
      { at: '2026-08-04T00:00:00Z', equity: 0, profitLoss: 0, profitLossPct: 0 },
      { at: '2026-08-05T00:00:00Z', equity: 100, profitLoss: 0, profitLossPct: 0 },
      { at: '2026-08-06T00:00:00Z', equity: 0, profitLoss: -100, profitLossPct: -100 },
    ]

    expect(equityCurve(wiped).map((p) => p.value)).toEqual([100, 0])
  })

  it('drops a point with no equity at all, which is not a balance of zero', () => {
    const gap: EquityCurvePoint[] = [
      { at: '2026-08-04T00:00:00Z', equity: 100, profitLoss: 0, profitLossPct: 0 },
      { at: '2026-08-05T00:00:00Z', equity: null, profitLoss: null, profitLossPct: null },
      { at: '2026-08-06T00:00:00Z', equity: 120, profitLoss: 20, profitLossPct: 20 },
    ]

    expect(equityCurve(gap).map((p) => p.date)).toEqual(['2026-08-04', '2026-08-06'])
  })

  it('takes the date off the UTC instant, not a local one', () => {
    // 04:30 UTC on the 5th is 00:30 ET on the 5th; late-evening ET stamps
    // are the half that would move a day if this went through a local Date.
    const points: EquityCurvePoint[] = [
      { at: '2026-08-05T00:30:00Z', equity: 100, profitLoss: 0, profitLossPct: 0 },
    ]

    expect(equityCurve(points)[0].date).toBe('2026-08-05')
  })
})

describe('realizedWinRate', () => {
  it('is the share of settled trades that won', () => {
    expect(realizedWinRate(STATS)).toBe(50)
    expect(realizedWinRate({ ...STATS, wins: 3, losses: 1 })).toBe(75)
  })

  it('is unknown, not zero, over an empty book', () => {
    expect(realizedWinRate(NOTHING_REALIZED)).toBeNull()
    expect(realizedWinRate(undefined)).toBeNull()
  })
})

describe('oldestRead', () => {
  it('reports the oldest read, so the line is a floor and not a boast', () => {
    expect(oldestRead([300, 100, 200])).toBe(100)
  })

  it('ignores the reads that have not landed', () => {
    expect(oldestRead([0, 0, 500])).toBe(500)
    expect(oldestRead([0, 0])).toBeNull()
  })
})

/* -------------------------------------------------------------------------
 * The page
 * ---------------------------------------------------------------------- */

describe('Header stats come off the broker, not the rows on screen', () => {
  it('shows the account equity and the broker’s own day figure', async () => {
    stubFetch()
    await renderDashboard()

    // Equity, not a sum of position values: client money arithmetic is
    // display-only and the server computes this one.
    expect(screen.getByText(formatUsd(PAPER.equity))).toBeInTheDocument()
    // Signed textually as well as by colour.
    expect(screen.getByText(formatUsd(PAPER.dayChange, { signed: true }))).toBeInTheDocument()
  })

  it('renders skeletons while the balance is in flight, not zeroes', async () => {
    stubFetch({ account: pending() })
    render(<App />)

    expect(await screen.findByText('Loading total balance')).toBeInTheDocument()
    expect(screen.getByText("Loading the day's change")).toBeInTheDocument()
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
  })

  it('states when the figures were read and offers a refresh, rather than claiming Live', async () => {
    const fetchMock = stubFetch()
    await renderDashboard()

    expect((await screen.findAllByText(/^Read /)).length).toBeGreaterThan(0)
    expect(within(section('Recent Executions')).queryByText(/Live/)).not.toBeInTheDocument()

    const before = fetchMock.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh the dashboard' }))
    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before))
  })
})

describe('Win rate stat', () => {
  it('reads the realized ledger and names how many trades it is over', async () => {
    stubFetch()
    await renderDashboard()

    const card = screen.getByRole('group', { name: 'Live win rate' })
    expect(within(card).getByText('50%')).toBeInTheDocument()
    expect(within(card).getByText(/4 realized trades/)).toBeInTheDocument()
    // Account-wide, and it says so: nothing in the Phase 2 ledger was
    // opened by a strategy, so a strategy-scoped figure would be invented.
    expect(within(card).getByText(/not one strategy/)).toBeInTheDocument()
  })

  it('shows an em dash over a book with nothing realized, not 0%', async () => {
    stubFetch({ stats: NOTHING_REALIZED })
    await renderDashboard()

    const card = screen.getByRole('group', { name: 'Live win rate' })
    expect(within(card).getByText('—')).toBeInTheDocument()
    expect(within(card).queryByText('0%')).not.toBeInTheDocument()
    expect(within(card).getByText(/unknown, not zero/)).toBeInTheDocument()
  })
})

describe('Recommended Trades has no source in this phase and says so', () => {
  it('reports the halt from /api/engine/state rather than showing fixtures', async () => {
    stubFetch()
    await renderDashboard()
    const panel = within(section('Recommended Trades'))

    expect(await panel.findByText('Engine halted')).toBeInTheDocument()
    expect(panel.getByText(/no candidates are being generated/)).toBeInTheDocument()
    // A cold-start halt carries no reason. Saying so beats an empty line.
    expect(panel.getByText(/comes up halted on every cold start/)).toBeInTheDocument()
  })

  it('states the reason and the time when the endpoint carries them', async () => {
    stubFetch({ engine: HALTED_WITH_REASON })
    await renderDashboard()
    const panel = within(section('Recommended Trades'))

    expect(await panel.findByText(/The Alpaca stream closed/)).toBeInTheDocument()
    expect(panel.getByText(/Halted since/)).toBeInTheDocument()
  })

  it('says the engine is running, and still empty, when it is not halted', async () => {
    stubFetch({ engine: RUNNING })
    await renderDashboard()
    const panel = within(section('Recommended Trades'))

    expect(await panel.findByText('Engine running')).toBeInTheDocument()
    expect(panel.getByText(/has generated no candidates/)).toBeInTheDocument()
    expect(panel.queryByText(/comes up halted/)).not.toBeInTheDocument()
  })

  it('carries no fixture candidate: no strike, no confidence, no Trade button', async () => {
    stubFetch()
    await renderDashboard()
    const panel = within(section('Recommended Trades'))

    expect(panel.queryByRole('button', { name: 'Trade' })).not.toBeInTheDocument()
    expect(panel.queryByRole('button', { name: 'Dismiss' })).not.toBeInTheDocument()
    // The Phase 1 panel carried confidence percentages on every row.
    expect(panel.queryByText(/^\d{2}%$/)).not.toBeInTheDocument()
  })

  it('says the request failed rather than claiming there are no candidates', async () => {
    stubFetch({ engine: jsonResponse(500, {}) })
    await renderDashboard()
    const panel = within(section('Recommended Trades'))

    expect(await panel.findByRole('alert')).toBeInTheDocument()
    expect(panel.queryByText(/no candidates are being generated/)).not.toBeInTheDocument()
  })
})

describe('Trading-state pill', () => {
  it('reads Manual on a cold start, because Manual is not a halt', async () => {
    stubFetch()
    await renderDashboard()

    expect(screen.getByText(/^Manual \(Paper\)$/)).toBeInTheDocument()
    expect(screen.queryByText(/Trading On/)).not.toBeInTheDocument()
  })

  it('reads Halted in Auto while the engine is halted, and Trading On when it is not', async () => {
    stubFetch()
    await renderDashboard()

    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    expect(screen.getByText(/^Halted \(Paper\)$/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Resume trading' }))
    expect(await screen.findByText(/^Trading On \(Paper\)$/)).toBeInTheDocument()
  })

  it('says it is still reading rather than guessing at the state', () => {
    stubFetch({ engine: pending() })
    render(<App />)

    expect(screen.getByText(/^Reading engine state \(Paper\)$/)).toBeInTheDocument()
    expect(screen.queryByText(/Trading On/)).not.toBeInTheDocument()
  })
})

describe('Halt and Resume are real engine state', () => {
  it('offers Resume in Manual too — a halt is not reachable only through Auto', async () => {
    // Rule 9: recovery from a halt needs an explicit human resume. The
    // dead-man's switch can halt while this page sits in Manual, and the
    // control that ends it must not be hidden behind an unrelated toggle.
    stubFetch()
    await renderDashboard()

    expect(screen.getByText(/^Manual \(Paper\)$/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Resume trading' })).toBeInTheDocument()
  })

  it('posts the resume to the engine rather than flipping a local flag', async () => {
    const writes: string[] = []
    stubFetch({ onEngineWrite: (path) => writes.push(path) })
    await renderDashboard()

    fireEvent.click(await screen.findByRole('button', { name: 'Resume trading' }))

    await waitFor(() => expect(writes).toEqual(['resume']))
    expect(await screen.findByText('Engine running')).toBeInTheDocument()
  })

  it('sends a reason with a halt, so the log records who asked for it', async () => {
    const bodies: unknown[] = []
    stubFetch({ engine: RUNNING, onEngineWrite: (_, body) => bodies.push(body) })
    await renderDashboard()

    fireEvent.click(await screen.findByRole('button', { name: 'Halt' }))

    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reason: expect.stringContaining('Dashboard') })
  })

  it('says so in error when the halt request fails', async () => {
    stubFetch({ engine: RUNNING })
    await renderDashboard()
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(500, {})))
    vi.stubGlobal('fetch', fetchMock)

    fireEvent.click(await screen.findByRole('button', { name: 'Halt' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})

describe('Flatten is inert in this phase, and says why', () => {
  it('is disabled with a stated reason naming Phase 6 and the open positions', async () => {
    stubFetch()
    await renderDashboard()

    expect(screen.getByRole('button', { name: 'Flatten' })).toBeDisabled()
    expect(screen.getByText(/Flatten is disabled in this phase/)).toBeInTheDocument()
    expect(screen.getByText(/arrives in Phase 6/)).toBeInTheDocument()
    // The count is the broker's, and it is named so the sentence is about
    // this account rather than about the feature.
    expect(await screen.findByText(/There are 2 open positions/)).toBeInTheDocument()
  })

  it('opens no confirm dialog, because there is nothing to confirm', async () => {
    stubFetch()
    await renderDashboard()

    fireEvent.click(screen.getByRole('button', { name: 'Flatten' }))
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

describe('Recent Executions', () => {
  it('renders the ledger window in the summary layout — P&L, not Status', async () => {
    stubFetch()
    await renderDashboard()
    const executions = within(section('Recent Executions'))

    // The summary layout prints the action and the contract in one cell
    // ("STC NVDA $240 Call Sep 11"), so this matches within the cell.
    expect(await executions.findByText(new RegExp(escaped(FILL_LOSS.contract)))).toBeInTheDocument()
    expect(executions.getByRole('columnheader', { name: 'P&L' })).toBeInTheDocument()
    expect(executions.getByRole('columnheader', { name: 'Price' })).toBeInTheDocument()
    expect(executions.queryByRole('columnheader', { name: 'Status' })).not.toBeInTheDocument()
  })

  it('stretches its rows so the panel ends level with Recommended Trades', async () => {
    stubFetch()
    await renderDashboard()
    await within(section('Recent Executions')).findByRole('table')

    expect(within(section('Recent Executions')).getByRole('table').className).toMatch(/h-full/)
  })

  it('leaves deposits and withdrawals off — they are not executions', async () => {
    stubFetch()
    await renderDashboard()
    const table = within(
      await within(section('Recent Executions')).findByRole('table'),
    )

    expect(table.queryByText('Deposit')).not.toBeInTheDocument()
    expect(table.queryByText('Withdrawal')).not.toBeInTheDocument()
    expect(table.queryByText('+$25,000.00')).not.toBeInTheDocument()
    // Not by hiding everything: the orders are still there.
    expect(table.getAllByRole('row').length).toBeGreaterThan(1)
  })

  it('leaves the rejection reason on hover here rather than inline', async () => {
    stubFetch()
    await renderDashboard()
    const table = within(await within(section('Recent Executions')).findByRole('table'))

    // Activity is the page of record for a rejection's rule (rule 8); this
    // panel is the summary layout and does not spell it out.
    expect(table.queryByText(/max_risk_per_trade_pct/)).not.toBeInTheDocument()
  })

  it('asks the server for the status filter instead of filtering the page it got', async () => {
    const sent: string[] = []
    stubFetch({ onActivity: (p) => sent.push(p.get('status') ?? 'none') })
    await renderDashboard()
    await within(section('Recent Executions')).findByRole('table')

    fireEvent.change(screen.getByRole('combobox', { name: 'Filter executions by status' }), {
      target: { value: 'rejected' },
    })

    await waitFor(() => expect(sent).toContain('rejected'))
    // And the first request asked for no status at all rather than "all",
    // which is not a status the server knows.
    expect(sent[0]).toBe('none')
  })

  it('over-fetches and caps at ten, so ten orders are ten orders', async () => {
    const many = Array.from({ length: 14 }, (_, i) => ({
      ...FILL_WIN,
      id: `fill-${i}`,
      contract: `AAPL $${300 + i} Call Dec 18`,
    }))
    const asked: string[] = []
    stubFetch({ ledger: [DEPOSIT, ...many], onActivity: (p) => asked.push(p.get('pageSize') ?? '') })
    await renderDashboard()
    const table = await within(section('Recent Executions')).findByRole('table')

    expect(asked[0]).toBe('30')
    // Header plus ten.
    expect(within(table).getAllByRole('row')).toHaveLength(11)
  })

  it('distinguishes a window of cash movements from an account that never traded', async () => {
    stubFetch({ ledger: [DEPOSIT, WITHDRAWAL] })
    await renderDashboard()
    const panel = within(section('Recent Executions'))

    expect(await panel.findByText(/all cash movements/)).toBeInTheDocument()
  })

  it('says the account has not traded when the ledger is genuinely empty', async () => {
    stubFetch({ ledger: [] })
    await renderDashboard()
    const panel = within(section('Recent Executions'))

    expect(await panel.findByText(/No orders in the Paper account yet/)).toBeInTheDocument()
  })

  it('shows a skeleton while the ledger is in flight', async () => {
    stubFetch({ ledger: pending() })
    render(<App />)

    expect(await screen.findByText('Loading recent executions')).toBeInTheDocument()
  })
})

describe('Performance is the broker’s curve, with t₀ marked', () => {
  it('plots from the day the account was funded, not from the zero pad', async () => {
    stubFetch()
    await renderDashboard()
    const panel = within(section('Performance'))

    expect(await panel.findByText(/from Aug 4, 2026/)).toBeInTheDocument()
  })

  it('does not claim the pre-Corollary stretch as the engine’s', async () => {
    stubFetch()
    await renderDashboard()
    const panel = within(section('Performance'))

    // t₀ is 2026-09-12 and the curve opens on 2026-08-04, so most of this
    // window is the account's own history (spec decision 6).
    expect(await panel.findByText(/predates Corollary’s first run/)).toBeInTheDocument()
    expect(panel.queryByText(/Entirely since/)).not.toBeInTheDocument()
  })

  it('says the curve has no shape yet rather than drawing a single point', async () => {
    stubFetch({ history: { ...HISTORY, points: HISTORY.points.slice(0, 4) } })
    await renderDashboard()
    const panel = within(section('Performance'))

    expect(await panel.findByText(/needs two closes/)).toBeInTheDocument()
  })

  it('shows a skeleton while the curve is in flight', async () => {
    stubFetch({ history: pending() })
    render(<App />)

    expect(await screen.findByText('Loading the equity curve')).toBeInTheDocument()
  })
})

describe('Cash without credentials', () => {
  it('renders the server’s reason and the missing variable names, not an idle account', async () => {
    stubFetch()
    await renderDashboard()

    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )

    expect(await screen.findByRole('heading', { name: 'Cash is not configured' })).toBeInTheDocument()
    expect(screen.getByText(/ALPACA_LIVE_API_KEY, ALPACA_LIVE_SECRET_KEY/)).toBeInTheDocument()
    // Paper's money is not on screen while a real-money account is selected.
    expect(screen.queryByText(formatUsd(PAPER.equity))).not.toBeInTheDocument()
  })
})
