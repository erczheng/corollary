import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { ApiError, isAccountUnavailable } from './api'
import { shouldRetry } from './queryClient'
import {
  queryKeys,
  useChain,
  useHaltEngine,
  usePositions,
  useRiskLimits,
} from './queries'
import { useUIStore } from './store'

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

/** Answer by URL, so a test can prove the *cash* request got the cash rows
 * rather than merely that something was fetched twice. */
function stubFetchByUrl(answer: (url: string) => Response) {
  const fetchMock = vi.fn((input: unknown) => Promise.resolve(answer(String(input))))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return { client, Wrapper }
}

afterEach(() => {
  vi.unstubAllGlobals()
  // Rule 5: paper is where every start comes up, and a leaked `cash` here
  // would be a test ordering bug that reads as a data bug.
  useUIStore.setState({ accountMode: 'paper' })
})

describe('query keys', () => {
  it('carry the account, so the two books are two cache entries', () => {
    expect(queryKeys.positions('paper')).not.toEqual(queryKeys.positions('cash'))
    expect(queryKeys.positions('paper')).toEqual(['positions', 'paper'])
  })

  it('put the account at index 1, so a prefix reaches one book only', () => {
    expect(queryKeys.activity('cash', { page: 3 })[1]).toBe('cash')
    expect(queryKeys.accountHistory('cash')[1]).toBe('cash')
    expect(queryKeys.workingOrders('cash')[1]).toBe('cash')
    expect(queryKeys.activityStats('cash')[1]).toBe('cash')
  })

  it('keep one length per question, so an omitted filter still invalidates', () => {
    expect(queryKeys.activity('paper')).toHaveLength(queryKeys.activity('paper', {
      page: 1,
      pageSize: 15,
      search: 'AAPL',
      status: 'filled',
    }).length)
    expect(queryKeys.activity('paper')).toEqual(['activity', 'paper', null, null, null, null])
  })

  it('separate a filtered ledger from an unfiltered one', () => {
    expect(queryKeys.activity('paper', { search: 'AAPL' })).not.toEqual(
      queryKeys.activity('paper'),
    )
  })

  /** A range control drives the request now, so the window has to be in the
   * key. Without it every range would collide on one entry and the second
   * range clicked would draw the first one's data. */
  it('carry the series window, so two ranges are two cache entries', () => {
    const day = queryKeys.underlyings(['AAPL'], { period: '1D', timeframe: '5Min' })
    const year = queryKeys.underlyings(['AAPL'], { period: '1A', timeframe: '1D' })

    expect(day).not.toEqual(year)
    expect(day).toEqual(['markets', 'underlyings', ['AAPL'], '1D', '5Min'])
    // Same length with no window as with one, so the unfiltered entry is
    // still reached by an invalidation of the filtered one.
    expect(queryKeys.underlyings(['AAPL'])).toHaveLength(day.length)
  })

  it('separate two windows that share a period but not a resolution', () => {
    // 1D at 5Min and 1D at 1D are 192 points and one point. Keyed on the
    // period alone they would be the same entry.
    expect(queryKeys.underlyings(['AAPL'], { period: '1D', timeframe: '5Min' })).not.toEqual(
      queryKeys.underlyings(['AAPL'], { period: '1D', timeframe: '1D' }),
    )
  })

  it('leave markets and settings unscoped by account', () => {
    expect(queryKeys.stocks()).toEqual(['markets', 'stocks', null])
    expect(queryKeys.riskLimits()).toEqual(['settings', 'limits'])
    expect(queryKeys.engineState()).toEqual(['engine', 'state'])
  })
})

describe('the selected book', () => {
  it('is read from the store, so no call site can forget it', async () => {
    useUIStore.setState({ accountMode: 'cash' })
    const fetchMock = stubFetchByUrl(() => jsonResponse(200, []))
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => usePositions(), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/positions?account=cash')
  })

  it('refetches on a switch rather than rendering the other book', async () => {
    const fetchMock = stubFetchByUrl((url) =>
      jsonResponse(200, [{ id: url.includes('cash') ? 'cash-row' : 'paper-row' }]),
    )
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => usePositions(), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.data?.[0].id).toBe('paper-row'))

    useUIStore.setState({ accountMode: 'cash' })

    await waitFor(() => expect(result.current.data?.[0].id).toBe('cash-row'))
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('can be overridden, for the paper read a cash page still needs', async () => {
    useUIStore.setState({ accountMode: 'cash' })
    const fetchMock = stubFetchByUrl(() => jsonResponse(200, []))
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => usePositions('paper'), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/positions?account=paper')
  })
})

describe('cash without credentials', () => {
  const reason =
    'The cash account is not configured: ALPACA_LIVE_KEY_ID and ' +
    'ALPACA_LIVE_SECRET_KEY are both required and both are absent.'

  it('reaches the page as a stated condition, never as an empty list', async () => {
    useUIStore.setState({ accountMode: 'cash' })
    stubFetchByUrl(() =>
      jsonResponse(409, { error: { code: 'account_unavailable', message: reason } }),
    )
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => usePositions(), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(result.current.data).toBeUndefined()
    expect(isAccountUnavailable(result.current.error)).toBe(true)
    expect(result.current.error?.message).toContain('ALPACA_LIVE_KEY_ID')
  })

  it('is not retried — the answer would be the same three times', () => {
    const unavailable = new ApiError({
      status: 409,
      code: 'account_unavailable',
      message: reason,
      url: '/api/positions?account=cash',
    })

    expect(shouldRetry(0, unavailable)).toBe(false)
  })

  it('still retries an unreachable engine once', () => {
    const unreachable = new ApiError({
      status: 0,
      code: 'network_unreachable',
      message: 'Could not reach the Corollary engine.',
      url: '/api/positions?account=paper',
    })

    expect(shouldRetry(0, unreachable)).toBe(true)
    expect(shouldRetry(1, unreachable)).toBe(false)
  })
})

describe('queries that wait for a reason to run', () => {
  it('does not fetch a chain for a row nobody expanded', async () => {
    const fetchMock = stubFetchByUrl(() => jsonResponse(200, []))
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => useChain(null), { wrapper: Wrapper })

    expect(result.current.fetchStatus).toBe('idle')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('fetches once a symbol is chosen', async () => {
    const fetchMock = stubFetchByUrl(() => jsonResponse(200, []))
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => useChain('NVDA', { type: 'call' }), {
      wrapper: Wrapper,
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/markets/chain/NVDA?type=call')
  })
})

describe('mutations', () => {
  it('writes the halt straight into the cache, so the pill flips at once', async () => {
    const halted = {
      halted: true,
      haltedReason: 'lost the Alpaca connection',
      haltedAt: '2026-09-12T14:00:00Z',
      t0: null,
    }
    stubFetchByUrl(() => jsonResponse(200, halted))
    const { client, Wrapper } = wrapper()

    const { result } = renderHook(() => useHaltEngine(), { wrapper: Wrapper })
    result.current.mutate('lost the Alpaca connection')
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(client.getQueryData(queryKeys.engineState())).toEqual(halted)
  })
})

describe('a ceiling nobody configured', () => {
  it('comes back as null and stays null — no substituted number', async () => {
    stubFetchByUrl(() =>
      jsonResponse(200, [
        {
          key: 'max_risk_per_trade_pct',
          label: 'Max risk per trade',
          value: null,
          unit: '%',
          min: 0.1,
          max: 25,
          help: 'Ceiling on the risk of a single position.',
        },
      ]),
    )
    const { Wrapper } = wrapper()

    const { result } = renderHook(() => useRiskLimits(), { wrapper: Wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(result.current.data?.[0].value).toBeNull()
  })
})
