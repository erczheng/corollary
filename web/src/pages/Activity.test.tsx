import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { formatUsd, formatPct } from '../lib/format'
import type {
  AccountResponse,
  ActivityItem,
  ActivityStats,
  Page,
  Position,
  WorkingOrder,
} from '../lib/types'

/** Activity reads five endpoints and holds no server state of its own.
 * These payloads are the shapes the live paper account actually returned on
 * 2026-09-12 — including `rejectionReason: null`, which is what the wire
 * sends and what the type had to be widened to accept.
 *
 * Nothing here comes from `mockData.ts`. The page is a data-source swap, so
 * a fixture imported from the Phase 1 file would prove the old rendering
 * against the old data and nothing about the new path. */

const PAGE_SIZE = 15

/** Folded server-side over **every** realized trade (spec decision 11).
 * Deliberately *unrelatable* to the rows in `LEDGER` below: the header
 * cards must read this endpoint, and a page that re-derived them from the
 * fifteen rows on screen would produce different numbers and fail. */
const STATS: ActivityStats = {
  avgWin: null,
  avgWinPct: null,
  avgLoss: -29.0,
  avgLossPct: -27.1616,
  lifetimePnl: -116.0,
  wins: 0,
  losses: 4,
  notBooked: 0,
  notBookedSymbols: [],
}

/** A book that has won and lost, so both averages render rather than one. */
const STATS_BOTH: ActivityStats = {
  ...STATS,
  avgWin: 412.5,
  avgWinPct: 33.25,
  wins: 2,
  lifetimePnl: 709.0,
}

/** The gap decision 14 exists for. `notBooked` is 0 on the live account
 * today, so this is the state nobody can see without a fixture. */
const STATS_GAP: ActivityStats = {
  ...STATS_BOTH,
  notBooked: 2,
  notBookedSymbols: ['GME1261016C00003000', 'GME1261016C00005000'],
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

const PENDING: ActivityItem = {
  id: '20260912000000000::c0ffee01',
  time: '2026-09-12T13:40:00.000000Z',
  contract: 'AMD $470/$460 Put Credit Spread Jan 15',
  action: 'STO',
  price: 3.4,
  quantity: 1,
  pnl: null,
  pnlPct: null,
  amount: null,
  status: 'pending',
  rejectionReason: null,
}

const LEDGER = [FILL_LOSS, FILL_WIN, REJECTED, PENDING, DEPOSIT, WITHDRAWAL]

const LONG_CALL: Position = {
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

const SHORT_SPREAD: Position = {
  id: '6dd8ab48-a705-464d-a92c-2543caa63989',
  symbol: 'AMD',
  contract: '$470/$460 Put Credit Spread Jan 15',
  last: -4.16,
  underlying: 516.115,
  costBasis: -340.0,
  value: -615.0,
  quantity: 1,
  pnl: -275.0,
  pnlPct: -80.8824,
  bid: -5.12,
  ask: -3.2,
  direction: 'short',
  legs: [
    { symbol: 'AMD270115P00470000', strike: 470.0, right: 'put', side: 'short', ratio: 1 },
    { symbol: 'AMD270115P00460000', strike: 460.0, right: 'put', side: 'long', ratio: 1 },
  ],
  expiry: '2027-01-15',
  strategyId: null,
  openedByStrategyId: null,
  managedExit: null,
  attachedExit: null,
  valueHistory: [{ date: '2026-09-11', value: -575.0 }],
}

const POSITIONS = [LONG_CALL, SHORT_SPREAD]

const WORKING: WorkingOrder = {
  id: 'wo-1',
  positionId: LONG_CALL.id,
  contractKey: null,
  contract: 'AAPL $340 Call Dec 18',
  side: 'STC',
  orderType: 'limit',
  quantity: 1,
  limitPrice: 18.5,
  stopPrice: null,
  timeInForce: 'gtc',
  placedAt: '2026-09-12T13:05:00.000000Z',
  activityId: '20260912000000000::c0ffee01',
}

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

function pageOf(items: ActivityItem[], page: number, total = items.length): Page<ActivityItem> {
  return {
    items,
    total,
    page,
    pageSize: PAGE_SIZE,
    hasMore: (page + 1) * PAGE_SIZE < total,
  }
}

interface Answers {
  stats?: ActivityStats | Response
  positions?: Position[] | Response
  working?: WorkingOrder[] | Response
  account?: Response
  /** Called with the parsed query of every `/api/activity` request, so a
   * test can assert what went *out* rather than only what came back — which
   * is the whole point of server-side search and paging. */
  onActivity?: (params: URLSearchParams) => void
  activity?: (params: URLSearchParams) => Response
}

function isResponse(value: unknown): value is Response {
  return typeof value === 'object' && value !== null && 'json' in value
}

/** Answer by URL. Order matters — `/activity/stats` is checked before
 * `/activity`, and `/positions/working` before `/positions`. */
function stubFetch(answers: Answers = {}) {
  const fetchMock = vi.fn((input: unknown, _init?: RequestInit) => {
    const url = String(input)
    const params = new URLSearchParams(url.slice(url.indexOf('?') + 1))

    if (url.includes('/activity/stats')) {
      const stats = answers.stats ?? STATS
      return Promise.resolve(isResponse(stats) ? stats : jsonResponse(200, stats))
    }
    if (url.includes('/activity')) {
      answers.onActivity?.(params)
      if (answers.activity) return Promise.resolve(answers.activity(params))
      return Promise.resolve(jsonResponse(200, pageOf(LEDGER, Number(params.get('page') ?? 0))))
    }
    if (url.includes('/positions/working')) {
      const working = answers.working ?? []
      return Promise.resolve(isResponse(working) ? working : jsonResponse(200, working))
    }
    if (url.includes('/positions')) {
      const positions = answers.positions ?? POSITIONS
      return Promise.resolve(isResponse(positions) ? positions : jsonResponse(200, positions))
    }
    if (url.includes('/account')) {
      if (url.includes('account=cash')) return Promise.resolve(answers.account ?? UNAVAILABLE)
      return Promise.resolve(jsonResponse(200, PAPER))
    }
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/activity')
  // Rule 5: every start is Paper. A leaked `cash` here would be a test
  // ordering bug that reads as a data bug.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  queryClient.clear()
})

function section(heading: string): HTMLElement {
  return screen.getByRole('heading', { name: heading }).closest('section')!
}

/** Render and wait for the ledger to land. Every row on the page is server
 * state now, so there is nothing to assert before it does. */
async function renderActivity() {
  render(<App />)
  await screen.findByRole('heading', { name: 'Recent Activity' })
  await screen.findByText(FILL_LOSS.contract)
}

function ledgerRows(): HTMLElement[] {
  return within(section('Recent Activity'))
    .getAllByRole('row')
    .slice(1)
}

function ledgerRow(text: string): HTMLElement {
  return ledgerRows().find((r) => r.textContent?.includes(text))!
}

/* ------------------------------------------------------------------------
 * Header cards — decision 11
 * --------------------------------------------------------------------- */

describe('Header stats', () => {
  it('reads the lifetime figures from the stats endpoint, not from the page on screen', async () => {
    const fetchMock = stubFetch({ stats: STATS_BOTH })
    await renderActivity()

    // The rows on screen carry a single -2.00 of realized P&L. The cards
    // show the server's fold over every trade, which is a different number
    // — that is the assertion.
    expect(
      await within(screen.getByRole('group', { name: 'Lifetime P&L' })).findByText(
        formatUsd(STATS_BOTH.lifetimePnl, { signed: true }),
      ),
    ).toBeInTheDocument()

    expect(
      fetchMock.mock.calls.some((c) => String(c[0]).includes('/activity/stats')),
    ).toBe(true)
  })

  it('averages wins and losses separately, in dollars and percent', async () => {
    stubFetch({ stats: STATS_BOTH })
    await renderActivity()

    const win = screen.getByRole('group', { name: 'Average win' })
    expect(within(win).getByText(formatUsd(STATS_BOTH.avgWin!, { signed: true }))).toBeInTheDocument()
    expect(within(win).getByText(formatPct(STATS_BOTH.avgWinPct!, { signed: true }))).toBeInTheDocument()

    const loss = screen.getByRole('group', { name: 'Average loss' })
    expect(
      within(loss).getByText(formatUsd(STATS_BOTH.avgLoss!, { signed: true })),
    ).toBeInTheDocument()
  })

  /** An average over zero trades is unknown, not zero. $0.00 would claim a
   * result that does not exist — and this is the live account's actual
   * state today: four losses and no wins. */
  it('shows an em dash rather than $0.00 where there is no trade of that kind', async () => {
    stubFetch({ stats: STATS })
    await renderActivity()

    const win = screen.getByRole('group', { name: 'Average win' })
    expect(within(win).getByText('—')).toBeInTheDocument()
    expect(within(win).queryByText('+$0.00')).not.toBeInTheDocument()
    expect(within(win).getByText('over 0 winning trades')).toBeInTheDocument()
  })

  /** A losing account is `bearish`, never `error`. A loss is not a system
   * failure, and the split has to survive the data being real. */
  it('colors a loss bearish and never error', async () => {
    stubFetch({ stats: STATS })
    await renderActivity()

    const lifetime = screen.getByRole('group', { name: 'Lifetime P&L' })
    const value = within(lifetime).getByText(formatUsd(STATS.lifetimePnl, { signed: true }))
    expect(value.className).toContain('text-bearish')
    expect(value.className).not.toContain('text-error')

    const loss = screen.getByRole('group', { name: 'Average loss' })
    expect(
      within(loss).getByText(formatUsd(STATS.avgLoss!, { signed: true })).className,
    ).toContain('text-bearish')
  })

  it('signs every figure textually as well as by color', async () => {
    stubFetch({ stats: STATS_BOTH })
    await renderActivity()

    expect(screen.getByText(/^\+\$412\.50$/)).toBeInTheDocument()
    expect(screen.getByText(/^−\$29\.00$/)).toBeInTheDocument()
  })

  it('shows skeletons while the fold is in flight, not an empty card', async () => {
    stubFetch({ stats: pending() })
    render(<App />)

    expect(await screen.findByText('Loading average win')).toBeInTheDocument()
    expect(screen.getByText('Loading lifetime P&L')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Lifetime P&L' })).not.toBeInTheDocument()
  })

  it('says the lifetime figures failed rather than showing a zero', async () => {
    stubFetch({ stats: jsonResponse(500, { error: { code: 'boom', message: 'Ledger unreadable.' } }) })
    render(<App />)

    expect(await screen.findByText('Ledger unreadable.', {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Lifetime P&L' })).not.toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * The gap the header cards admit to — decision 14
 * --------------------------------------------------------------------- */

describe('Closings the lifetime figures are missing', () => {
  it('names the count and the contracts beside the cards', async () => {
    stubFetch({ stats: STATS_GAP })
    await renderActivity()

    const gap = await screen.findByRole('region', {
      name: '2 closings are missing from the figures above',
    })
    expect(within(gap).getByText(/GME1261016C00003000, GME1261016C00005000/)).toBeInTheDocument()
  })

  /** The cause is not stored by any Phase 2 table (decision 14 — it lands
   * at step 8). A confident wrong reason on a money figure is worse than an
   * admitted gap, so the panel says the count and stops. */
  it('does not assert a reason it cannot evidence', async () => {
    stubFetch({ stats: STATS_GAP })
    await renderActivity()

    const gap = screen.getByRole('region', {
      name: '2 closings are missing from the figures above',
    })
    expect(within(gap).getByText(/reason is not recorded yet/)).toBeInTheDocument()
    expect(gap.textContent).not.toMatch(/adjusted deliverable|assignment|exercise/i)
  })

  it('says nothing at all on a complete ledger', async () => {
    stubFetch({ stats: STATS })
    await renderActivity()

    expect(screen.queryByText(/missing from the figures above/)).not.toBeInTheDocument()
  })

  /** Singular has its own sentence: "1 closings" in a panel about money
   * reads as a bug in the panel and invites doubt about the figure. */
  it('reads as English for a single missing closing', async () => {
    stubFetch({ stats: { ...STATS_GAP, notBooked: 1, notBookedSymbols: ['GME1261016C00003000'] } })
    await renderActivity()

    expect(
      await screen.findByRole('region', { name: '1 closing is missing from the figures above' }),
    ).toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * The ledger table
 * --------------------------------------------------------------------- */

describe('Recent Activity columns', () => {
  it('runs Time, Asset, Action, P&L, Price, Qty, Status — in that order', async () => {
    stubFetch()
    await renderActivity()

    const headers = within(section('Recent Activity'))
      .getAllByRole('columnheader')
      .map((h) => h.textContent)
    expect(headers).toEqual(['Time', 'Asset', 'Action', 'P&L', 'Price', 'Qty', 'Status'])
  })

  it('renders the status word, which the summary layout can only put on hover', async () => {
    stubFetch()
    await renderActivity()

    expect(within(ledgerRow(REJECTED.contract)).getByText('Rejected')).toBeInTheDocument()
  })

  /** PRD §8.2 and rule 8: this is the page of record for a rejection, so
   * the rule that rejected it is inline, not on hover. `error`, not
   * `bearish` — a rule outcome, not a losing position. */
  it('spells out the rejection rule inline, in error', async () => {
    stubFetch()
    await renderActivity()

    const reason = within(ledgerRow(REJECTED.contract)).getByText(REJECTED.rejectionReason!)
    expect(reason.className).toContain('text-error')
    expect(reason.className).not.toContain('text-bearish')
  })

  /** The wire sends `null`, not an absent key. A filled row must not grow
   * an empty reason line out of it. */
  it('shows no reason line where the server sent a null one', async () => {
    stubFetch()
    await renderActivity()

    const row = ledgerRow(FILL_WIN.contract)
    expect(row.querySelector('.text-error')).toBeNull()
  })

  it('signs a realized loss textually and paints it bearish', async () => {
    stubFetch()
    await renderActivity()

    const pnl = within(ledgerRow(FILL_LOSS.contract)).getByText('−$2.00')
    expect(pnl.className).toContain('text-bearish')
    expect(pnl.className).not.toContain('text-error')
  })

  it('leaves P&L an em dash on an opening fill, which has realized nothing', async () => {
    stubFetch()
    await renderActivity()

    expect(within(ledgerRow(FILL_WIN.contract)).getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('Cash movements in the ledger', () => {
  it('shows the money moved on a deposit and a withdrawal, signed', async () => {
    stubFetch()
    await renderActivity()

    expect(within(ledgerRow('Deposit')).getByText('+$25,000.00')).toBeInTheDocument()
    expect(within(ledgerRow('Withdrawal')).getByText('−$1,500.00')).toBeInTheDocument()
  })

  /** Money you moved in is not money the account made. Rendering it
   * bullish green would read as a gain. */
  it('does not color a cash movement as if it were a gain or a loss', async () => {
    stubFetch()
    await renderActivity()

    const amount = within(ledgerRow('Deposit')).getByText('+$25,000.00')
    expect(amount.className).toContain('text-on-surface')
    expect(amount.className).not.toContain('bullish')
  })

  it('leaves the price column empty on a cash movement rather than showing zero', async () => {
    stubFetch()
    await renderActivity()

    expect(within(ledgerRow('Deposit')).queryByText('$0.00')).not.toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * Search and filter are the server's, not this page's
 * --------------------------------------------------------------------- */

describe('Searching the ledger', () => {
  it('sends the search to the server rather than filtering the page in hand', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({ onActivity: (p) => seen.push(p) })
    await renderActivity()

    fireEvent.change(screen.getByLabelText('Search activity by symbol or contract'), {
      target: { value: 'NVDA' },
    })

    await waitFor(() => {
      expect(seen.some((p) => p.get('search') === 'NVDA')).toBe(true)
    })
  })

  /** AND, not OR. "Everything I did in AAPL that was rejected" is one
   * question, and the server answers it as one. */
  it('combines the search with the status filter in a single request', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({ onActivity: (p) => seen.push(p) })
    await renderActivity()

    fireEvent.change(screen.getByLabelText('Search activity by symbol or contract'), {
      target: { value: 'TSLA' },
    })
    fireEvent.change(screen.getByLabelText('Filter activity by status'), {
      target: { value: 'rejected' },
    })

    await waitFor(() => {
      expect(
        seen.some((p) => p.get('search') === 'TSLA' && p.get('status') === 'rejected'),
      ).toBe(true)
    })
  })

  it('omits the parameters entirely when the question is not being asked', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({ onActivity: (p) => seen.push(p) })
    await renderActivity()

    expect(seen[0].has('search')).toBe(false)
    expect(seen[0].has('status')).toBe(false)
  })

  it('explains an empty result instead of looking like an empty account', async () => {
    stubFetch({
      activity: (p) =>
        p.get('search') === 'ZZZZ'
          ? jsonResponse(200, pageOf([], 0, 0))
          : jsonResponse(200, pageOf(LEDGER, 0)),
    })
    await renderActivity()

    fireEvent.change(screen.getByLabelText('Search activity by symbol or contract'), {
      target: { value: 'ZZZZ' },
    })

    expect(await screen.findByText(/Nothing matching/)).toBeInTheDocument()
    expect(screen.getByText(/clear it to see the rest of the ledger/)).toBeInTheDocument()
  })

  it('returns to the first page when the search changes', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({
      onActivity: (p) => seen.push(p),
      activity: () => jsonResponse(200, pageOf(LEDGER, 0, 40)),
    })
    await renderActivity()

    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(seen.some((p) => p.get('page') === '1')).toBe(true))

    seen.length = 0
    fireEvent.change(screen.getByLabelText('Search activity by symbol or contract'), {
      target: { value: 'AAPL' },
    })

    await waitFor(() => {
      expect(seen.some((p) => p.get('search') === 'AAPL' && p.get('page') === '0')).toBe(true)
    })
    // No searched request asked for a page other than the first. Narrowing
    // while deep in the feed used to land on the last page of the new
    // result set, which reads as "no results".
    expect(
      seen.filter((p) => p.get('search') === 'AAPL').every((p) => p.get('page') === '0'),
    ).toBe(true)
  })
})

/* ------------------------------------------------------------------------
 * Paging — the one-based control over a zero-based API
 * --------------------------------------------------------------------- */

describe('Paging the ledger', () => {
  it('asks the server for page 0 first and shows it as page 1', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({
      onActivity: (p) => seen.push(p),
      activity: () => jsonResponse(200, pageOf(LEDGER, 0, 40)),
    })
    await renderActivity()

    expect(seen[0].get('page')).toBe('0')
    expect(seen[0].get('pageSize')).toBe(String(PAGE_SIZE))
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
  })

  /** The trap this page exists to avoid: `usePagination` is one-based and
   * slices an array in hand, the API is zero-based and slices server-side.
   * Handing the control's number straight to the query skips page one. */
  it('asks for page 1 when the reader asks for page 2', async () => {
    const seen: URLSearchParams[] = []
    stubFetch({
      onActivity: (p) => seen.push(p),
      activity: () => jsonResponse(200, pageOf(LEDGER, 0, 40)),
    })
    await renderActivity()

    fireEvent.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() => expect(seen.some((p) => p.get('page') === '1')).toBe(true))
    expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()
  })

  /** The page count comes off `total`, which is every matching row — not
   * off `items.length`, which is one page and would always say "Page 1 of
   * 1" however deep the ledger went. */
  it('counts the pages from the total, not from the rows on screen', async () => {
    stubFetch({ activity: () => jsonResponse(200, pageOf(LEDGER, 0, 31)) })
    await renderActivity()

    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
  })

  it('hides the pager when one page is the whole ledger', async () => {
    stubFetch()
    await renderActivity()

    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * Loading, failure, and the book that cannot be read
 * --------------------------------------------------------------------- */

describe('Loading and failure', () => {
  it('shows skeletons rather than an empty book while the ledger is in flight', async () => {
    stubFetch({ activity: () => pending(), positions: pending() })
    render(<App />)

    expect(await screen.findByText('Loading activity')).toBeInTheDocument()
    expect(screen.getByText('Loading open positions')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing in the Paper ledger/)).not.toBeInTheDocument()
  })

  /** A failed request is `error` — a system condition. An empty table would
   * claim the account has never traded, which is the one thing a failed
   * request cannot know. */
  it('says the ledger request failed, in error, rather than showing no rows', async () => {
    stubFetch({
      activity: () =>
        jsonResponse(503, { error: { code: 'upstream', message: 'The ledger is unavailable.' } }),
    })
    render(<App />)

    const alert = await screen.findByText('The ledger is unavailable.', {}, { timeout: 4000 })
    expect(alert.className).toContain('text-error')
    expect(screen.queryByText(/Nothing in the Paper ledger/)).not.toBeInTheDocument()
  })

  it('says the engine did not answer when nothing answered at all', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))),
    )
    render(<App />)

    expect(await screen.findAllByText(/did not answer/, {}, { timeout: 4000 })).not.toHaveLength(0)
  })

  /** Rule 5 and the 409: serving paper's ledger under the cash account's
   * name would misreport which money moved. */
  it('renders the cash 409 as a designed state naming what is missing', async () => {
    stubFetch({
      activity: () => UNAVAILABLE,
      stats: UNAVAILABLE,
      positions: UNAVAILABLE,
      working: UNAVAILABLE,
    })
    useUIStore.setState({ accountMode: 'cash' })
    render(<App />)

    const banner = await screen.findByRole('region', { name: 'Cash is not configured' })
    expect(within(banner).getByText(/ALPACA_LIVE_API_KEY, ALPACA_LIVE_SECRET_KEY/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Recent Activity' })).not.toBeInTheDocument()
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
  })

  it('never renders an empty ledger for a book it cannot read', async () => {
    stubFetch({
      activity: () => UNAVAILABLE,
      stats: UNAVAILABLE,
      positions: UNAVAILABLE,
      working: UNAVAILABLE,
    })
    useUIStore.setState({ accountMode: 'cash' })
    render(<App />)

    await screen.findByRole('region', { name: 'Cash is not configured' })
    expect(screen.queryByText(/has not traded/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Nothing in the Cash ledger/)).not.toBeInTheDocument()
  })
})

describe('An empty ledger', () => {
  /** An empty feed before the open means the day has not started. The same
   * feed at 3pm means the day produced nothing. Different facts. */
  it('says the market has not opened yet, before the open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-09-14T12:00:00Z')) // 08:00 ET
    stubFetch({ activity: () => jsonResponse(200, pageOf([], 0, 0)) })
    render(<App />)

    expect(await screen.findByText(/market has not opened yet today/)).toBeInTheDocument()
  })

  it('says the book has not traded, once the session is under way', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(new Date('2026-09-14T19:00:00Z')) // 15:00 ET
    stubFetch({ activity: () => jsonResponse(200, pageOf([], 0, 0)) })
    render(<App />)

    expect(await screen.findByText(/has not traded rather than one whose day/)).toBeInTheDocument()
  })

  it('distinguishes an empty filter from an empty book', async () => {
    stubFetch({
      activity: (p) =>
        p.get('status') === 'canceled'
          ? jsonResponse(200, pageOf([], 0, 0))
          : jsonResponse(200, pageOf(LEDGER, 0)),
    })
    await renderActivity()

    fireEvent.change(screen.getByLabelText('Filter activity by status'), {
      target: { value: 'canceled' },
    })

    expect(await screen.findByText(/No canceled activity in the Paper account/)).toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * Read time, not a Live pill
 * --------------------------------------------------------------------- */

describe('How current the figures are', () => {
  /** `store.tick()` was the Phase 1 mock broker. This page is polled now,
   * and a "Live" badge over refetched data claims more than is true. */
  it('states the time the ledger was read, and claims nothing about streaming', async () => {
    stubFetch()
    await renderActivity()

    const panel = section('Recent Activity')
    expect(panel.textContent).toMatch(/Read \d{1,2}:\d{2}\s?(AM|PM) ET/)
    expect(within(panel).queryByText('Live')).not.toBeInTheDocument()
    expect(within(panel).queryByText('Connecting')).not.toBeInTheDocument()
  })

  it('refetches every panel on the page when refreshed', async () => {
    const fetchMock = stubFetch()
    await renderActivity()

    const before = fetchMock.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Refresh the ledger' }))

    await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before))
  })
})

/* ------------------------------------------------------------------------
 * Positions and working orders
 * --------------------------------------------------------------------- */

describe('Open Positions', () => {
  it('renders one row per position the broker reports', async () => {
    stubFetch()
    await renderActivity()

    const panel = section('Open Positions')
    expect(await within(panel).findByText(/\$340 Call Dec 18/)).toBeInTheDocument()
    expect(within(panel).getByText(/\$470\/\$460 Put Credit Spread Jan 15/)).toBeInTheDocument()
    expect(within(panel).getByText('2 open in Paper')).toBeInTheDocument()
  })

  it('signs unrealized P&L textually and paints a loser bearish', async () => {
    stubFetch()
    await renderActivity()

    const panel = section('Open Positions')
    expect(within(panel).getByText('+$340.00').className).toContain('text-bullish')
    const loser = within(panel).getByText('−$275.00')
    expect(loser.className).toContain('text-bearish')
    expect(loser.className).not.toContain('text-error')
  })

  it('explains an empty book rather than showing a bare table', async () => {
    stubFetch({ positions: [] })
    await renderActivity()

    expect(
      within(section('Open Positions')).getByText(/No open positions in this account/),
    ).toBeInTheDocument()
  })

  it('says positions failed to load rather than reporting none held', async () => {
    stubFetch({
      positions: jsonResponse(503, {
        error: { code: 'upstream', message: 'Positions are unavailable.' },
      }),
    })
    await renderActivity()

    const panel = section('Open Positions')
    expect(await within(panel).findByText('Positions are unavailable.')).toBeInTheDocument()
    expect(within(panel).queryByText('0 open in Paper')).not.toBeInTheDocument()
  })
})

describe('Working Orders', () => {
  it('lists the orders resting at the broker', async () => {
    stubFetch({ working: [WORKING] })
    await renderActivity()

    const panel = section('Working Orders')
    expect(await within(panel).findByText(WORKING.contract)).toBeInTheDocument()
    expect(within(panel).getByText('1 working in Paper')).toBeInTheDocument()
  })

  /** A cancel would be the first broker write in this codebase and would
   * land before the risk manager exists. Disabled with the reason stated,
   * never a button that silently does nothing. */
  it('disables Cancel and says why, rather than pretending it works', async () => {
    stubFetch({ working: [WORKING] })
    await renderActivity()

    const panel = section('Working Orders')
    const cancel = await within(panel).findByRole('button', { name: 'Cancel' })
    expect(cancel).toBeDisabled()
    expect(within(panel).getByText(/not wired up in this phase/)).toBeInTheDocument()
  })

  it('shows an empty state on an account with none, pointing at where exits live', async () => {
    stubFetch({ working: [] })
    await renderActivity()

    expect(
      within(section('Working Orders')).getByText(/Exits attached to a position live/),
    ).toBeInTheDocument()
  })

  it('says working orders failed to load rather than reporting none resting', async () => {
    stubFetch({
      working: jsonResponse(503, {
        error: { code: 'upstream', message: 'Working orders are unavailable.' },
      }),
    })
    await renderActivity()

    const panel = section('Working Orders')
    expect(await within(panel).findByText('Working orders are unavailable.')).toBeInTheDocument()
  })
})

/* ------------------------------------------------------------------------
 * The ticket is an estimate surface in this phase
 * --------------------------------------------------------------------- */

describe('The order ticket, read-only', () => {
  function expand() {
    const row = within(section('Open Positions'))
      .getAllByRole('row')
      .find((r) => r.textContent?.includes('AAPL $340 Call Dec 18'))!
    fireEvent.click(within(row).getByRole('button', { name: 'Close' }))
  }

  it('still states bid, ask and the estimate, which are live', async () => {
    stubFetch()
    await renderActivity()
    expand()

    expect(screen.getByText('Bid / ask')).toBeInTheDocument()
    expect(
      screen.getByText(`${formatUsd(LONG_CALL.bid)} / ${formatUsd(LONG_CALL.ask)}`),
    ).toBeInTheDocument()
  })

  /** Phase 2 sends no order. The control is disabled with the reason in
   * words rather than removed — a missing button does not say why it is
   * missing, and the estimate beside it is still worth reading. */
  it('cannot be submitted, and says why in words', async () => {
    stubFetch()
    await renderActivity()
    expand()

    const submit = screen.getByRole('button', { name: 'Review close' })
    expect(submit).toBeDisabled()
    expect(screen.getByText(/no order reaches the broker/)).toBeInTheDocument()
  })

  /** Enter in a form field submits the form, so the disabled button alone
   * would still let the keyboard open a confirm for an order nothing can
   * place. */
  it('does not open the confirm on Enter either', async () => {
    stubFetch()
    await renderActivity()
    expand()

    const qty = screen.getByLabelText('Quantity')
    fireEvent.submit(qty.closest('form')!)

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('never submits an order to the server', async () => {
    const fetchMock = stubFetch()
    await renderActivity()
    expand()

    fireEvent.click(screen.getByRole('button', { name: 'Review close' }))
    expect(
      fetchMock.mock.calls.every((c) => {
        const init = c[1]
        return init?.method === undefined || init.method === 'GET'
      }),
    ).toBe(true)
  })
})

/* ------------------------------------------------------------------------
 * Account scoping
 * --------------------------------------------------------------------- */

describe('Account scoping', () => {
  it('asks every endpoint about the selected book', async () => {
    const fetchMock = stubFetch()
    await renderActivity()

    const urls = fetchMock.mock.calls.map((c) => String(c[0]))
    expect(urls.some((u) => u.includes('/activity?') && u.includes('account=paper'))).toBe(true)
    expect(urls.some((u) => u.includes('/activity/stats') && u.includes('account=paper'))).toBe(true)
    expect(urls.some((u) => u.includes('/positions?') && u.includes('account=paper'))).toBe(true)
    expect(urls.some((u) => u.includes('/positions/working') && u.includes('account=paper'))).toBe(
      true,
    )
  })
})
