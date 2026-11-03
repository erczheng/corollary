import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import App from '../App'
import { AccountModeToggle } from './AccountModeToggle'
import { useUIStore } from '../lib/store'
import { queryClient } from '../lib/queryClient'
import { formatUsd } from '../lib/format'
import { cashOutsideBuyingPower } from '../lib/account'
import { CASH_SESSION_CHECK_TIMEOUT_MS, confirmCash, resetCashSessionChecks } from '../lib/cashSession'
import { ACCOUNT_SNAPSHOTS } from '../lib/mockData'
import type { AccountResponse, CashSessionState } from '../lib/types'

/* -------------------------------------------------------------------------
 * Harness
 *
 * The live cash account is a fixture distinct from `ACCOUNT_SNAPSHOTS` on
 * purpose: the dialog must quote what `GET /api/account?account=cash` says,
 * and a fixture that matched the mock would let the old mock-reading dialog
 * pass these tests.
 * ---------------------------------------------------------------------- */

const MASK = '••••1234'

const LIVE: AccountResponse = {
  account: 'cash',
  status: 'ACTIVE',
  currency: 'USD',
  cash: 18_420.55,
  equity: 21_337.12,
  lastEquity: 21_100,
  dayChange: 237.12,
  balanceTrend: null,
  buyingPower: 18_100.55,
  optionsBuyingPower: 18_100.55,
  longMarketValue: 2_916.57,
  shortMarketValue: 0,
  netPositionValue: 2_916.57,
  grossPositionValue: 2_916.57,
  derivedEquity: 21_337.12,
  equityReconciles: true,
  equityDifference: 0,
  margin: {} as AccountResponse['margin'],
  optionsApprovedLevel: 3,
  optionsTradingLevel: 3,
  tradingBlocked: false,
  accountBlocked: false,
  transfersBlocked: false,
  cashAccountAvailable: true,
  cashAccountUnavailableReason: null,
  missingLiveCredentialEnvVars: [],
  accountNumberMasked: MASK,
}

const ACTIVE: CashSessionState = {
  active: true,
  accountNumberMasked: MASK,
  confirmedAt: '2026-10-23T14:00:00Z',
  balance: '21337.12',
}

const INACTIVE: CashSessionState = {
  active: false,
  accountNumberMasked: null,
  confirmedAt: null,
  balance: null,
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function refusal(status: number, code: string, message: string): Response {
  return json(status, { error: { code, message } })
}

interface Deferred {
  promise: Promise<Response>
  resolve: (r: Response) => void
}

function deferred(): Deferred {
  let resolve!: (r: Response) => void
  const promise = new Promise<Response>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

type Answer = () => Promise<Response> | Response

interface Routes {
  cashAccount?: Answer
  post?: Answer
  del?: Answer
  session?: Answer
}

interface Call {
  method: string
  url: string
  body: unknown
}

function stubFetch(routes: Routes = {}) {
  const calls: Call[] = []
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ method, url, body })
    if (url.includes('/account/cash-session')) {
      if (method === 'POST') return Promise.resolve((routes.post ?? (() => json(200, ACTIVE)))())
      if (method === 'DELETE')
        return Promise.resolve((routes.del ?? (() => new Response(null, { status: 204 })))())
      return Promise.resolve((routes.session ?? (() => json(200, INACTIVE)))())
    }
    if (url.includes('/account?account=cash')) {
      return Promise.resolve((routes.cashAccount ?? (() => json(200, LIVE)))())
    }
    // Everything else behaves as it does with no engine at all — the world
    // every other `<App />` suite already renders in.
    return Promise.reject(new TypeError('fetch failed'))
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    calls,
    of: (method: string) => calls.filter((c) => c.method === method && c.url.includes('/cash-session')),
  }
}

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState }, true)
  queryClient.clear()
  resetCashSessionChecks()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function renderToggle() {
  return render(
    <QueryClientProvider client={queryClient}>
      <AccountModeToggle />
    </QueryClientProvider>,
  )
}

function dialog(): HTMLElement {
  return screen.getByRole('alertdialog')
}

function confirmButton(): HTMLElement {
  return within(dialog()).getByRole('button', { name: /Switch to Cash|Confirming/ })
}

async function openDialogReady() {
  fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
  await waitFor(() => expect(confirmButton()).toBeEnabled())
}

/* -------------------------------------------------------------------------
 * Cold start
 * ---------------------------------------------------------------------- */

describe('AccountModeToggle', () => {
  it('comes up in Paper on a cold start', () => {
    stubFetch()
    render(<App />)

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(within(screen.getByRole('main')).getByRole('button', { name: 'Paper' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('does not switch on the click alone — it asks first', () => {
    stubFetch()
    renderToggle()
    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(dialog()).toBeInTheDocument()
  })

  /* -----------------------------------------------------------------------
   * The confirm names the real live account
   * -------------------------------------------------------------------- */

  describe('the confirm', () => {
    it('reads the live cash account, not the mock', async () => {
      const net = stubFetch()
      renderToggle()
      await openDialogReady()

      expect(net.calls.some((c) => c.method === 'GET' && c.url.includes('/account?account=cash'))).toBe(
        true,
      )
      const text = dialog().textContent ?? ''
      expect(text).toContain(MASK)
      expect(text).toContain(formatUsd(LIVE.equity))
      expect(text).toContain(formatUsd(LIVE.buyingPower))
      expect(text).toMatch(/real money/)
      // The fixtures are not the source any more.
      expect(text).not.toContain(formatUsd(ACCOUNT_SNAPSHOTS.cash.buyingPower))
    })

    it('renders the mask and the balance in tabular mono', async () => {
      stubFetch()
      renderToggle()
      await openDialogReady()

      expect(within(dialog()).getByText(MASK)).toHaveClass('font-mono', 'tabular-nums')
      expect(within(dialog()).getByText(formatUsd(LIVE.equity))).toHaveClass('font-mono', 'tabular-nums')
    })

    it('states the gap between cash and buying power, with no reason', async () => {
      stubFetch()
      renderToggle()
      await openDialogReady()

      const text = dialog().textContent ?? ''
      const gap = formatUsd(cashOutsideBuyingPower(LIVE.cash, LIVE.buyingPower))
      expect(text).toContain(`of which ${gap} is not counted in buying power`)
      expect(text).not.toMatch(/spendable|unsettled|yours/)
      expect(within(dialog()).getByText(gap)).toHaveClass('font-mono', 'tabular-nums')
    })

    it('states no gap when buying power covers the cash', async () => {
      stubFetch({ cashAccount: () => json(200, { ...LIVE, buyingPower: LIVE.cash }) })
      renderToggle()
      await openDialogReady()

      expect(dialog().textContent ?? '').not.toMatch(/not counted in buying power/)
    })

    it('quotes options buying power, the figure an options order is bound by', async () => {
      stubFetch({ cashAccount: () => json(200, { ...LIVE, optionsBuyingPower: 9_876.5 }) })
      renderToggle()
      await openDialogReady()

      expect(dialog().textContent ?? '').toMatch(/options buying power/)
      expect(within(dialog()).getByText(formatUsd(9_876.5))).toHaveClass('font-mono', 'tabular-nums')
    })

    it('says in words when options buying power is not reported — never $0', async () => {
      stubFetch({ cashAccount: () => json(200, { ...LIVE, optionsBuyingPower: null }) })
      renderToggle()
      await openDialogReady()

      const text = dialog().textContent ?? ''
      expect(text).toMatch(/did not report options buying power/)
      expect(text).not.toContain(formatUsd(0))
    })

    it('says it is reading, and holds the confirm shut, while the account loads', () => {
      const held = deferred()
      stubFetch({ cashAccount: () => held.promise })
      renderToggle()
      fireEvent.click(screen.getByRole('button', { name: 'Cash' }))

      expect(within(dialog()).getByRole('status')).toHaveTextContent(/Reading the live account/)
      expect(confirmButton()).toBeDisabled()
    })

    it.each([
      [
        'not configured (409)',
        () => refusal(409, 'account_unavailable', 'ALPACA_LIVE_API_KEY is not set.'),
        /Cash is not configured: ALPACA_LIVE_API_KEY is not set\./,
      ],
      [
        'unavailable (503)',
        () => refusal(503, 'cash_unavailable', 'No live credentials are configured.'),
        /could not be read: No live credentials are configured\./,
      ],
      [
        'a vendor error (502)',
        () => refusal(502, 'broker_unavailable', 'Alpaca answered 500.'),
        /could not be read: Alpaca answered 500\./,
      ],
      ['unreachable', () => Promise.reject(new TypeError('fetch failed')), /engine did not answer/],
    ])('says so plainly and cannot confirm when the account is %s', async (_, answer, expected) => {
      stubFetch({ cashAccount: answer as Answer })
      renderToggle()
      fireEvent.click(screen.getByRole('button', { name: 'Cash' }))

      const alert = await within(dialog()).findByRole('alert')
      expect(alert).toHaveTextContent(expected)
      expect(alert).toHaveTextContent(/Cash cannot be confirmed/)
      expect(alert).toHaveClass('text-error')
      expect(confirmButton()).toBeDisabled()
    })

    it('cannot confirm an account that reported no number', async () => {
      stubFetch({ cashAccount: () => json(200, { ...LIVE, accountNumberMasked: null }) })
      renderToggle()
      fireEvent.click(screen.getByRole('button', { name: 'Cash' }))

      expect(await within(dialog()).findByRole('alert')).toHaveTextContent(/no account number/)
      expect(confirmButton()).toBeDisabled()
    })

    it('leaves the account alone when cancelled, and sends nothing', async () => {
      const net = stubFetch()
      renderToggle()
      await openDialogReady()
      fireEvent.click(within(dialog()).getByRole('button', { name: 'Cancel' }))

      expect(useUIStore.getState().accountMode).toBe('paper')
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
      expect(net.of('POST')).toHaveLength(0)
    })
  })

  /* -----------------------------------------------------------------------
   * Confirming
   * -------------------------------------------------------------------- */

  describe('confirming', () => {
    it('posts the masked number the dialog showed, and switches only on the 200', async () => {
      const held = deferred()
      const net = stubFetch({ post: () => held.promise })
      renderToggle()
      await openDialogReady()

      fireEvent.click(confirmButton())

      await waitFor(() => expect(net.of('POST')).toHaveLength(1))
      expect(net.of('POST')[0].body).toEqual({ accountNumber: MASK })
      // In flight: still Paper, and nothing can be clicked twice or raced.
      expect(useUIStore.getState().accountMode).toBe('paper')
      expect(confirmButton()).toBeDisabled()
      expect(confirmButton()).toHaveTextContent(/Confirming/)
      expect(within(dialog()).getByRole('button', { name: 'Cancel' })).toBeDisabled()

      await act(async () => {
        held.resolve(json(200, ACTIVE))
        await held.promise
      })

      await waitFor(() => expect(useUIStore.getState().accountMode).toBe('cash'))
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    })

    it.each([
      [409, 'cash_account_mismatch', 'The account number does not match the live account.'],
      [409, 'cash_session_ended_while_confirming', 'The Cash session was ended while confirming.'],
      [503, 'cash_unavailable', 'No live credentials are configured.'],
      [502, 'broker_unavailable', 'Alpaca could not be reached.'],
    ])('a %i %s keeps Paper and shows the server message as an error', async (status, code, message) => {
      stubFetch({ post: () => refusal(status, code, message) })
      renderToggle()
      await openDialogReady()

      fireEvent.click(confirmButton())

      const alert = await within(dialog()).findByRole('alert')
      expect(alert).toHaveTextContent(message)
      expect(alert).toHaveTextContent(/still in Paper/)
      expect(alert).toHaveClass('text-error')
      expect(alert).not.toHaveClass('text-bearish')
      expect(useUIStore.getState().accountMode).toBe('paper')
    })

    it('a 200 that is not active does not switch', async () => {
      stubFetch({ post: () => json(200, INACTIVE) })
      renderToggle()
      await openDialogReady()
      fireEvent.click(confirmButton())

      expect(await within(dialog()).findByRole('alert')).toHaveTextContent(/without confirming/)
      expect(useUIStore.getState().accountMode).toBe('paper')
    })

    it('cannot be submitted twice', async () => {
      const held = deferred()
      const net = stubFetch({ post: () => held.promise })
      renderToggle()
      await openDialogReady()

      // Two clicks in the same tick, before React can re-render the button
      // disabled — the store's pending flag is the guard that catches this.
      const button = confirmButton()
      fireEvent.click(button)
      fireEvent.click(button)
      fireEvent.click(button)

      await waitFor(() => expect(useUIStore.getState().cashSessionPending).toBe(true))
      expect(net.of('POST')).toHaveLength(1)

      await act(async () => {
        held.resolve(json(200, ACTIVE))
        await held.promise
      })
      await waitFor(() => expect(useUIStore.getState().cashSessionPending).toBe(false))
      expect(net.of('POST')).toHaveLength(1)
    })

    it('disables the Paper switch while the confirm is in flight, so no DELETE races it', async () => {
      const held = deferred()
      const net = stubFetch({ post: () => held.promise })
      renderToggle()
      await openDialogReady()
      fireEvent.click(confirmButton())
      await waitFor(() => expect(useUIStore.getState().cashSessionPending).toBe(true))

      const paper = screen.getByRole('button', { name: 'Paper', hidden: true })
      expect(paper).toBeDisabled()
      fireEvent.click(paper)
      expect(net.of('DELETE')).toHaveLength(0)

      await act(async () => {
        held.resolve(json(200, ACTIVE))
        await held.promise
      })
    })
  })

  /* -----------------------------------------------------------------------
   * Back to Paper
   * -------------------------------------------------------------------- */

  describe('switching to Paper', () => {
    it('switches at once, without a confirm, then ends the session', async () => {
      const held = deferred()
      const net = stubFetch({ del: () => held.promise })
      useUIStore.setState({ accountMode: 'cash' })
      renderToggle()

      fireEvent.click(screen.getByRole('button', { name: 'Paper' }))

      // Immediately — the DELETE is still unanswered.
      expect(useUIStore.getState().accountMode).toBe('paper')
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
      expect(net.of('DELETE')).toHaveLength(1)

      await act(async () => {
        held.resolve(new Response(null, { status: 204 }))
        await held.promise
      })
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('reports a failed DELETE plainly, stays in Paper, and can retry', async () => {
      let fail = true
      const net = stubFetch({
        del: () =>
          fail ? refusal(502, 'broker_unavailable', 'Alpaca could not be reached.') : new Response(null, { status: 204 }),
      })
      useUIStore.setState({ accountMode: 'cash' })
      renderToggle()

      fireEvent.click(screen.getByRole('button', { name: 'Paper' }))

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent(/did not confirm it ended the Cash session/)
      expect(alert).toHaveTextContent(/Alpaca could not be reached\./)
      expect(alert).toHaveClass('text-error')
      expect(useUIStore.getState().accountMode).toBe('paper')

      fail = false
      fireEvent.click(within(alert).getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
      expect(net.of('DELETE')).toHaveLength(2)
    })

    it('sends nothing when Paper is already selected', () => {
      const net = stubFetch()
      renderToggle()
      fireEvent.click(screen.getByRole('button', { name: 'Paper' }))

      expect(net.of('DELETE')).toHaveLength(0)
    })
  })

  /* -----------------------------------------------------------------------
   * Rule 5: never persisted
   * -------------------------------------------------------------------- */

  it('never writes the account mode to storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    stubFetch()
    renderToggle()
    await openDialogReady()
    fireEvent.click(confirmButton())
    await waitFor(() => expect(useUIStore.getState().accountMode).toBe('cash'))
    fireEvent.click(screen.getByRole('button', { name: 'Paper' }))
    await waitFor(() => expect(useUIStore.getState().accountMode).toBe('paper'))

    expect(setItem).not.toHaveBeenCalled()
  })
})

/* -------------------------------------------------------------------------
 * Audit item 3: the boot DELETE cannot be overtaken by the operator's POST
 * ---------------------------------------------------------------------- */

describe('while a session check is in flight', () => {
  it('disables the Cash pill and the confirm, and leaves Paper live', async () => {
    stubFetch()
    renderToggle()
    await openDialogReady()

    act(() => useUIStore.setState({ cashSessionReconciling: true }))
    expect(confirmButton()).toBeDisabled()
    expect(within(dialog()).getByRole('status')).toHaveTextContent(/Checking the engine/)
    act(() => useUIStore.setState({ cashSessionReconciling: false }))
    await waitFor(() => expect(confirmButton()).toBeEnabled())
  })

  it('holds the Cash pill across the check GET, so no POST can be sent before it answers', async () => {
    const held = deferred()
    const net = stubFetch({ session: () => held.promise })
    render(<App />)

    const cashPill = within(screen.getByRole('main')).getByRole('button', { name: 'Cash' })
    await waitFor(() => expect(cashPill).toBeDisabled())
    expect(within(screen.getByRole('main')).getByRole('button', { name: 'Paper' })).toBeEnabled()
    // The second line: a caller with no button is refused too.
    await expect(confirmCash(MASK)).resolves.toBeNull()
    expect(net.of('POST')).toHaveLength(0)

    await act(async () => held.resolve(json(200, ACTIVE)))
    await waitFor(() => expect(net.of('DELETE')).toHaveLength(1))
    await waitFor(() => expect(cashPill).toBeEnabled())
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('unlocks after the timeout when the engine never answers', async () => {
    vi.useFakeTimers()
    try {
      stubFetch({ session: () => new Promise<Response>(() => {}) })
      render(<App />)
      const cashPill = within(screen.getByRole('main')).getByRole('button', { name: 'Cash' })
      expect(cashPill).toBeDisabled()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(CASH_SESSION_CHECK_TIMEOUT_MS - 1)
      })
      expect(cashPill).toBeDisabled()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1)
      })
      expect(cashPill).toBeEnabled()
      expect(useUIStore.getState().accountMode).toBe('paper')
    } finally {
      vi.useRealTimers()
    }
  })
})
