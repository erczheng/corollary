import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, act, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { formatUsd } from '../lib/format'
import type { AccountResponse, ActivityItem, Page, Position } from '../lib/types'

/** The Account page reads four endpoints and nothing else — no fixtures, no
 * store-held server state. These payloads are copies of what the live paper
 * account actually returned on 2026-09-12, trimmed of nothing, so a shape
 * that drifts on the server shows up here rather than in a browser.
 *
 * The figures are the real ones and they reconcile: 53,386.08 cash +
 * 46,515.00 net position value = 99,901.08 equity, which is the number Alpaca
 * reports. That is the arithmetic the page puts on screen, so a fixture that
 * did not add up would make the reconciliation test meaningless. */
const PAPER: AccountResponse = {
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
    note:
      'The broker reports a multiplier of 4 — a pattern day-trader margin account — so buying ' +
      'power is 4× cash. Options are not marginable, so an option order sizes against options ' +
      'buying power, never against this figure.',
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
}

/** A configured Cash book, for the one thing that cannot be tested with the
 * 409: that switching accounts moves every figure. Different money, a cash
 * margin class, and no options entitlement reported. */
const CASH: AccountResponse = {
  ...PAPER,
  account: 'cash',
  cash: 12480.55,
  equity: 15230.55,
  lastEquity: 15100.55,
  dayChange: 130.0,
  balanceTrend: { changePct: 0.861, comparedTo: 'vs previous close' },
  buyingPower: 12480.55,
  optionsBuyingPower: null,
  longMarketValue: 2750.0,
  shortMarketValue: 0,
  netPositionValue: 2750.0,
  grossPositionValue: 2750.0,
  derivedEquity: 15230.55,
  equityReconciles: true,
  equityDifference: 0,
  margin: {
    multiplier: 1,
    marginClass: 'cash',
    label: 'Cash account',
    note: 'The broker reports a multiplier of 1 — a cash account, with no margin at all.',
  },
  cashAccountAvailable: true,
  cashAccountUnavailableReason: null,
  missingLiveCredentialEnvVars: [],
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
  amount: 100000,
  status: 'filled',
}

const WITHDRAWAL: ActivityItem = {
  ...DEPOSIT,
  id: '20260806000000000::9b1f',
  time: '2026-08-06T14:02:00Z',
  action: 'WITHDRAWAL',
  amount: -2500,
}

/** An order row, included deliberately: `cashTransfers` filters it out, and
 * a transfers table that listed a fill would be stating that a trade moved
 * money into the account. */
const FILL: ActivityItem = {
  ...DEPOSIT,
  id: 'fill-1',
  contract: 'AAPL $340 Call Dec 18',
  action: 'BTO',
  price: 12.55,
  quantity: 1,
  amount: null,
}

const POSITIONS = [
  { id: 'a', value: 1595 },
  { id: 'b', value: -615 },
  { id: 'c', value: 45535 },
] as unknown as Position[]

function transferPage(items: ActivityItem[], total = items.length): Page<ActivityItem> {
  return { items, total, page: 0, pageSize: 100, hasMore: total > items.length }
}

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

const UNAVAILABLE = jsonResponse(409, {
  error: {
    code: 'account_unavailable',
    message:
      'The cash account is not configured: ALPACA_LIVE_API_KEY and ALPACA_LIVE_SECRET_KEY are ' +
      'both required and neither is set.',
  },
})

interface Answers {
  paper?: Response
  cash?: Response
  positions?: Response
  transfers?: Response
  cashTransfers?: Response
}

/** Answer by URL, so a test can prove the *cash* request got the cash book
 * rather than merely that something was fetched twice. */
function stubFetch(answers: Answers = {}) {
  const fetchMock = vi.fn((input: unknown) => {
    const url = String(input)
    if (url.includes('/account/transfers')) {
      if (url.includes('account=cash')) {
        return Promise.resolve(answers.cashTransfers ?? answers.transfers ?? transfersOk())
      }
      return Promise.resolve(answers.transfers ?? transfersOk())
    }
    if (url.includes('/positions')) {
      return Promise.resolve(answers.positions ?? jsonResponse(200, POSITIONS))
    }
    if (url.includes('/account')) {
      if (url.includes('account=cash')) return Promise.resolve(answers.cash ?? UNAVAILABLE)
      return Promise.resolve(answers.paper ?? jsonResponse(200, PAPER))
    }
    // Nothing else on this page fetches. A 200 with an empty body keeps an
    // unexpected call from reading as the failure under test.
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function transfersOk(): Response {
  return jsonResponse(200, transferPage([DEPOSIT, WITHDRAWAL, FILL]))
}

/** A promise that never settles — the loading state, held open. */
function pending(): Response {
  return new Promise<never>(() => {}) as unknown as Response
}

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/account')
  // Rule 5: every start is Paper. A leaked `cash` here would be a test
  // ordering bug that reads as a data bug.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

function panel(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

/** Render and wait for the account request to land. Every figure on the page
 * is server-side now, so there is nothing to assert before it does. */
async function renderAccount() {
  render(<App />)
  await screen.findByRole('group', { name: 'Cash' })
}

describe('Account balances', () => {
  it('shows cash, day change, buying power and options buying power', async () => {
    stubFetch()
    await renderAccount()

    expect(
      within(screen.getByRole('group', { name: 'Cash' })).getByText(formatUsd(PAPER.cash)),
    ).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Buying power' })).getByText(
        formatUsd(PAPER.buyingPower),
      ),
    ).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Options buying power' })).getByText(
        formatUsd(PAPER.optionsBuyingPower ?? 0),
      ),
    ).toBeInTheDocument()
  })

  /** Sign is textual as well as coloured, and a losing day is `bearish` —
   * never `error`. A falling balance is not a system failure. */
  it('signs the day change and paints it bearish, not error', async () => {
    stubFetch()
    await renderAccount()

    const card = screen.getByRole('group', { name: 'Day change' })
    const value = within(card).getByText(formatUsd(PAPER.dayChange, { signed: true }))
    expect(value).toHaveTextContent('−')
    expect(value.className).toContain('text-bearish')
    expect(value.className).not.toContain('text-error')
  })

  /** Options are not marginable, so this note is not decoration: sizing an
   * option order against equity buying power overstates capacity several
   * times over. */
  it('says options are not marginable', async () => {
    stubFetch()
    await renderAccount()
    expect(screen.getByText('Options are not marginable')).toBeInTheDocument()
  })

  /** The regression this migration exists for. The page hard-coded "Margin
   * account — 2× cash"; the live paper account reports `multiplier: 4`. At 4×
   * that sentence is wrong in the direction that overstates capacity, so the
   * margin sentence is now built server-side from the real multiplier and
   * rendered verbatim. */
  it('quotes the broker’s margin class rather than asserting a multiplier', async () => {
    stubFetch()
    await renderAccount()

    expect(screen.getByText(PAPER.margin.label)).toBeInTheDocument()
    expect(screen.getByText(PAPER.margin.note)).toBeInTheDocument()
    expect(screen.queryByText(/2× cash/)).not.toBeInTheDocument()
  })

  /** Absent is not zero. `optionsBuyingPower` is nullable on the broker's own
   * object, and a substituted $0.00 would report an options trader as having
   * no capacity at all. */
  it('says options buying power is not reported rather than showing $0.00', async () => {
    stubFetch({ cash: jsonResponse(200, CASH), cashTransfers: transfersOk() })
    await renderAccount()
    act(() => useUIStore.setState({ accountMode: 'cash' }))

    const card = await screen.findByRole('group', { name: 'Options buying power' })
    await waitFor(() => expect(within(card).getByText('Not reported')).toBeInTheDocument())
    expect(within(card).queryByText('$0.00')).not.toBeInTheDocument()
  })

  /** Rendering one account's money while the other is selected is the exact
   * failure CLAUDE.md calls out for account-scoped state. The query key
   * carries the account, so the two books are two cache entries. */
  it('shows the other account’s money once Cash is selected', async () => {
    stubFetch({ cash: jsonResponse(200, CASH), cashTransfers: transfersOk() })
    await renderAccount()
    act(() => useUIStore.setState({ accountMode: 'cash' }))

    // The cards skeleton while the other book is in flight rather than
    // holding paper's figures under Cash's heading — that substitution is
    // the failure this whole scoping exists to prevent.
    expect(screen.queryByRole('group', { name: 'Cash' })).not.toBeInTheDocument()

    const card = await screen.findByRole('group', { name: 'Cash' })
    await waitFor(() => expect(within(card).getByText(formatUsd(CASH.cash))).toBeInTheDocument())
    expect(within(card).queryByText(formatUsd(PAPER.cash))).not.toBeInTheDocument()
  })

  /** PRD.md §2: the switch belongs on any page whose entire contents are
   * account-scoped, which this page is more completely than any other. */
  it('carries the Paper/Cash switch, not just the header badge', async () => {
    stubFetch()
    await renderAccount()
    const main = screen.getByRole('main')
    expect(within(main).getByRole('button', { name: 'Paper' })).toBeInTheDocument()
    expect(within(main).getByRole('button', { name: 'Cash' })).toBeInTheDocument()
  })
})

/** The settled/unsettled panel was removed with the fields behind it — the
 * live account object publishes no settlement breakdown at all, now confirmed
 * against the real endpoint rather than merely absent from the docs. */
describe('settlement', () => {
  it('claims no settlement breakdown Alpaca does not supply', async () => {
    stubFetch()
    await renderAccount()
    expect(screen.queryByRole('region', { name: 'Settled vs unsettled' })).not.toBeInTheDocument()
    expect(screen.queryByText(/good-faith violation/)).not.toBeInTheDocument()
  })
})

describe('total equity', () => {
  it('reads the broker’s equity and the broker’s net position value', async () => {
    stubFetch()
    await renderAccount()
    const p = panel('Total equity')

    expect(within(p).getByText(formatUsd(PAPER.cash))).toBeInTheDocument()
    expect(within(p).getByText(formatUsd(PAPER.netPositionValue))).toBeInTheDocument()
    expect(within(p).getByText(formatUsd(PAPER.equity))).toBeInTheDocument()
  })

  /** `netPositionValue` is long + short. The broker also publishes
   * `position_market_value`, which is |long| + |short| and overstates equity
   * by twice the short market value — zero error on a book holding no shorts,
   * which is why it has to be named rather than left to be mistaken. */
  it('breaks the position value into long and short, and names gross separately', async () => {
    stubFetch()
    await renderAccount()
    const p = panel('Total equity')

    expect(within(p).getByText(formatUsd(PAPER.longMarketValue))).toBeInTheDocument()
    expect(
      within(p).getByText(formatUsd(PAPER.shortMarketValue, { signed: true })),
    ).toHaveTextContent('−')
    expect(within(p).getByText(formatUsd(PAPER.grossPositionValue ?? 0))).toBeInTheDocument()
    expect(within(p).getByText(/not a term in the equity above/)).toBeInTheDocument()
  })

  it('counts the open positions from the positions endpoint', async () => {
    stubFetch()
    await renderAccount()
    expect(await within(panel('Total equity')).findByText('3 held')).toBeInTheDocument()
  })

  /** Two sources for one figure, and the page says whether they agree rather
   * than picking one silently. */
  it('says the two equity figures agree when they do', async () => {
    stubFetch()
    await renderAccount()
    expect(within(panel('Total equity')).getByText(/Two sources, one number/)).toBeInTheDocument()
  })

  it('states the gap, in caution, when they do not', async () => {
    stubFetch({
      paper: jsonResponse(200, {
        ...PAPER,
        derivedEquity: 99801.08,
        equityReconciles: false,
        equityDifference: -100,
      } satisfies AccountResponse),
    })
    await renderAccount()

    const note = within(panel('Total equity')).getByText(/difference of/)
    expect(note).toHaveTextContent(formatUsd(-100, { signed: true }))
    expect(note).toHaveTextContent(/authoritative/)
    // Not `error`: the broker and the arithmetic disagreeing is a condition to
    // surface, not a failed request.
    expect(note.className).toContain('text-caution')
    expect(note.className).not.toContain('text-error')
  })

  /** `isPending` replaced `lastTickAt === null`. Before the first response
   * there is no honest figure to print — showing a zero would read as a
   * flat, empty book. */
  it('skeletons every figure until the first response arrives', async () => {
    stubFetch({ paper: pending(), positions: pending(), transfers: pending() })
    render(<App />)

    expect(await screen.findByText('Reading cash balance')).toBeInTheDocument()
    expect(screen.getByText('Reading open positions')).toBeInTheDocument()
    expect(screen.getByText('Reading equity')).toBeInTheDocument()
    expect(screen.getByText('Loading buying power')).toBeInTheDocument()
    expect(screen.getByText('Reading cash transfers')).toBeInTheDocument()
    expect(screen.queryByText(formatUsd(PAPER.equity))).not.toBeInTheDocument()
  })

  it('says when the balances were read, rather than claiming a live stream', async () => {
    stubFetch()
    await renderAccount()
    const p = panel('Total equity')

    expect(within(p).getByText(/Read/)).toBeInTheDocument()
    expect(
      within(p).getByRole('button', { name: 'Refresh account balances' }),
    ).toBeInTheDocument()
    // These are polled balances, not a 400ms position stream. A "Live" pill
    // over them would claim more than is true.
    expect(within(p).queryByRole('status')).not.toBeInTheDocument()
  })
})

/** The 409 exists so that an unreadable book never renders as an empty one.
 * A `$0.00` equity where the truth is "we cannot see this account"
 * misreports real money. */
describe('a cash account with no credentials', () => {
  it('names what is missing instead of showing an empty balance', async () => {
    stubFetch()
    await renderAccount()
    act(() => useUIStore.setState({ accountMode: 'cash' }))

    expect(await screen.findByText('Cash is not configured')).toBeInTheDocument()
    expect(screen.getByText(/ALPACA_LIVE_API_KEY, ALPACA_LIVE_SECRET_KEY/)).toBeInTheDocument()
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Total equity' })).not.toBeInTheDocument()
  })

  /** Not an error state: nothing failed, the account is simply not
   * configured. `caution`, and the switch back to Paper stays on screen. */
  it('reads as a stated condition, not a failure', async () => {
    stubFetch()
    await renderAccount()
    act(() => useUIStore.setState({ accountMode: 'cash' }))

    const banner = await screen.findByRole('region', { name: 'Cash is not configured' })
    expect(banner.className).toContain('caution')
    expect(banner.className).not.toContain('error')
    expect(screen.getByRole('main')).toHaveTextContent('Showing the Cash account')
    expect(within(screen.getByRole('main')).getByRole('button', { name: 'Paper' })).toBeInTheDocument()
  })
})

describe('a request that fails', () => {
  it('says the engine did not answer, in error', async () => {
    const fetchMock = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')))
    vi.stubGlobal('fetch', fetchMock)
    render(<App />)

    // Balances and transfers are separate requests and both miss, so both
    // panels say so. Neither is allowed to render a number instead.
    const alerts = await screen.findAllByText(/did not answer/, {}, { timeout: 4000 })
    const alert = alerts[0]
    expect(alerts).toHaveLength(2)
    expect(alert.className).toContain('text-error')
    // A failed request is not a losing position.
    expect(alert.className).not.toContain('text-bearish')
  })

  it('quotes the server’s stated condition for anything else', async () => {
    stubFetch({
      paper: jsonResponse(500, { error: { code: 'broker_error', message: 'Alpaca returned 503.' } }),
    })
    render(<App />)
    expect(await screen.findByText('Alpaca returned 503.', {}, { timeout: 3000 })).toBeInTheDocument()
  })
})

describe('cash transfers', () => {
  it('lists deposits and withdrawals and excludes orders', async () => {
    stubFetch()
    await renderAccount()
    const table = await within(panel('Cash transfers')).findByRole('table')

    // Two transfers, plus the header row and the footer. The fill is filtered.
    expect(within(table).getAllByRole('row')).toHaveLength(4)
    expect(within(table).getByText('Deposit')).toBeInTheDocument()
    expect(within(table).getByText('Withdrawal')).toBeInTheDocument()
    expect(within(table).queryByText(/AAPL/)).not.toBeInTheDocument()
  })

  /** Sign is textual as well as coloured — colour alone fails in grayscale
   * and for colourblind readers. */
  it('signs every amount explicitly', async () => {
    stubFetch()
    await renderAccount()
    const table = await within(panel('Cash transfers')).findByRole('table')

    expect(within(table).getByText(formatUsd(100000, { signed: true }))).toHaveTextContent('+')
    expect(within(table).getByText(formatUsd(-2500, { signed: true }))).toHaveTextContent('−')
  })

  it('nets the transfers in a footer row', async () => {
    stubFetch()
    await renderAccount()
    const table = await within(panel('Cash transfers')).findByRole('table')

    expect(within(table).getByText('Net transferred')).toBeInTheDocument()
    expect(within(table).getByText(formatUsd(97500, { signed: true }))).toBeInTheDocument()
  })

  /** A page of a longer ledger nets that page. Calling that "Net
   * transferred" would state a figure about the book that is simply wrong. */
  it('renames the footer and says so when the ledger is longer than the page', async () => {
    stubFetch({ transfers: jsonResponse(200, transferPage([DEPOSIT, WITHDRAWAL], 240)) })
    await renderAccount()
    const p = panel('Cash transfers')

    expect(await within(p).findByText('Net of the transfers shown')).toBeInTheDocument()
    expect(within(p).queryByText('Net transferred')).not.toBeInTheDocument()
    expect(within(p).getByText(/most recent 2 of 240 transfers/)).toBeInTheDocument()
  })

  it('links out to Alpaca rather than offering a transfer form', async () => {
    stubFetch()
    await renderAccount()
    const link = within(panel('Cash transfers')).getByRole('link', {
      name: /Deposit or withdraw at Alpaca/,
    })
    expect(link).toHaveAttribute('href', 'https://app.alpaca.markets/')
  })

  it('says paper transfers are simulated', async () => {
    stubFetch()
    await renderAccount()
    expect(await screen.findByText(/Paper transfers are simulated/)).toBeInTheDocument()
  })

  /** A designed empty state, not a blank table. No transfers in a book that
   * has traded means something specific — the balance got here through P&L. */
  it('explains an empty ledger instead of rendering an empty table', async () => {
    stubFetch({ transfers: jsonResponse(200, transferPage([])) })
    await renderAccount()

    const p = panel('Cash transfers')
    expect(await within(p).findByText(/balance has moved through trading alone/)).toBeInTheDocument()
    expect(within(p).queryByRole('table')).not.toBeInTheDocument()
  })

  it('reports a failed transfers request without blanking the balances', async () => {
    stubFetch({ transfers: jsonResponse(409, { error: { code: 'x', message: 'Ledger is down.' } }) })
    await renderAccount()

    expect(await within(panel('Cash transfers')).findByText('Ledger is down.')).toBeInTheDocument()
    // The balances came from a different request and are still true.
    expect(
      within(screen.getByRole('group', { name: 'Cash' })).getByText(formatUsd(PAPER.cash)),
    ).toBeInTheDocument()
  })
})
