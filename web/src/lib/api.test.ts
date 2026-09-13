import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ACCOUNT_UNAVAILABLE,
  API_BASE,
  ApiError,
  HTTP_ERROR,
  NETWORK_UNREACHABLE,
  apiUrl,
  fetchAccount,
  fetchAccountHistory,
  fetchActivity,
  fetchActivityStats,
  fetchAuditLog,
  fetchChain,
  fetchPositions,
  fetchStocks,
  fetchTransfers,
  fetchUnderlyings,
  fetchWorkingOrders,
  haltEngine,
  isAccountUnavailable,
  isApiError,
  isUnreachable,
  resumeEngine,
  updateDataFeeds,
  updateNotificationRoutes,
  updateRiskLimits,
  wireMoney,
} from './api'
import type { AccountMode } from './types'

/** A response stub carrying only what `request` touches. Built by hand
 * rather than from the platform `Response`, so the tests do not depend on
 * which of jsdom or Node supplies the global. */
function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function opaqueResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: () => Promise.reject(new SyntaxError('Unexpected token < in JSON')),
  } as unknown as Response
}

function errorResponse(status: number, code: string, message: string): Response {
  return jsonResponse(status, { error: { code, message } })
}

/** Install a fetch that answers everything with one body, and hand back the
 * spy so a test can read the URL and the request init. */
function stubFetch(response: Response | (() => Promise<Response>)) {
  const produce = typeof response === 'function' ? response : () => Promise.resolve(response)
  // Declared with fetch's arity so the recorded calls carry the URL and the
  // init a test then reads.
  const fetchMock = vi.fn((_input: unknown, _init?: unknown) => produce())
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function calledUrl(fetchMock: ReturnType<typeof stubFetch>, call = 0): string {
  return String(fetchMock.mock.calls[call][0])
}

function calledInit(fetchMock: ReturnType<typeof stubFetch>, call = 0): RequestInit {
  return fetchMock.mock.calls[call][1] as RequestInit
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('apiUrl', () => {
  it('is relative by default, so the terminal never names a host', () => {
    expect(API_BASE).toBe('/api')
    expect(apiUrl('/positions')).toBe('/api/positions')
  })

  it('drops null and undefined rather than sending them as words', () => {
    expect(apiUrl('/activity', { account: 'paper', search: null, status: undefined })).toBe(
      '/api/activity?account=paper',
    )
  })

  it('escapes values that would otherwise change the query', () => {
    expect(apiUrl('/activity', { search: 'AAPL 240&x' })).toBe(
      '/api/activity?search=AAPL+240%26x',
    )
  })
})

describe('the account boundary', () => {
  /** Every account-scoped endpoint, called once per book. A missing
   * `?account` would be served as paper by the server — correct for the
   * server, and a misreport here the moment Cash is selected. */
  const scoped: ReadonlyArray<[string, (account: AccountMode) => Promise<unknown>, string]> = [
    ['account', (account) => fetchAccount(account), '/api/account'],
    ['account history', (account) => fetchAccountHistory(account), '/api/account/history'],
    ['transfers', (account) => fetchTransfers(account), '/api/account/transfers'],
    ['activity', (account) => fetchActivity(account), '/api/activity'],
    ['activity stats', (account) => fetchActivityStats(account), '/api/activity/stats'],
    ['positions', (account) => fetchPositions(account), '/api/positions'],
    ['working orders', (account) => fetchWorkingOrders(account), '/api/positions/working'],
  ]

  it.each(scoped)('%s names the book in the query', async (_label, call, path) => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await call('cash')
    await call('paper')

    expect(calledUrl(fetchMock, 0)).toContain(`${path}?account=cash`)
    expect(calledUrl(fetchMock, 1)).toContain(`${path}?account=paper`)
  })

  it('leaves markets unscoped — one symbol has one price in both books', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchStocks()
    await fetchChain('NVDA')

    expect(calledUrl(fetchMock, 0)).toBe('/api/markets/stocks')
    expect(calledUrl(fetchMock, 1)).toBe('/api/markets/chain/NVDA')
  })
})

describe('a cash book with no credentials', () => {
  const reason =
    'The cash account is not configured: ALPACA_LIVE_KEY_ID and ' +
    'ALPACA_LIVE_SECRET_KEY are both required and both are absent.'

  it('surfaces as its own condition, not as an empty ledger', async () => {
    stubFetch(errorResponse(409, ACCOUNT_UNAVAILABLE, reason))

    const failure = await fetchPositions('cash').catch((error: unknown) => error)

    expect(isApiError(failure)).toBe(true)
    expect(isAccountUnavailable(failure)).toBe(true)
    expect((failure as ApiError).status).toBe(409)
    expect((failure as ApiError).code).toBe(ACCOUNT_UNAVAILABLE)
  })

  it('keeps the stated reason verbatim, since it names what is missing', async () => {
    stubFetch(errorResponse(409, ACCOUNT_UNAVAILABLE, reason))

    const failure = (await fetchActivity('cash').catch((error: unknown) => error)) as ApiError

    expect(failure.message).toBe(reason)
    expect(failure.message).toContain('ALPACA_LIVE_KEY_ID')
  })

  it('is not confused with any other failure', async () => {
    stubFetch(errorResponse(422, 'invalid_history_window', 'the unit for a year is A, not Y'))

    const failure = (await fetchAccountHistory('paper', { period: '1Y' }).catch(
      (error: unknown) => error,
    )) as ApiError

    expect(isAccountUnavailable(failure)).toBe(false)
    expect(failure.code).toBe('invalid_history_window')
    expect(failure.status).toBe(422)
  })
})

describe('failures that are not the envelope', () => {
  it('keeps the status when the body is a proxy page', async () => {
    stubFetch(opaqueResponse(502))

    const failure = (await fetchPositions('paper').catch((error: unknown) => error)) as ApiError

    expect(failure.status).toBe(502)
    expect(failure.code).toBe(HTTP_ERROR)
    expect(failure.message).toContain('502')
  })

  it('says the engine is unreachable when there was no response at all', async () => {
    stubFetch(() => Promise.reject(new TypeError('Failed to fetch')))

    const failure = (await fetchPositions('paper').catch((error: unknown) => error)) as ApiError

    expect(isUnreachable(failure)).toBe(true)
    expect(failure.status).toBe(0)
    expect(failure.code).toBe(NETWORK_UNREACHABLE)
  })

  it('lets an abort stay an abort, so a cancelled query is not a failure', async () => {
    const aborted = new Error('The operation was aborted.')
    aborted.name = 'AbortError'
    stubFetch(() => Promise.reject(aborted))

    const failure = await fetchPositions('paper').catch((error: unknown) => error)

    expect(failure).toBe(aborted)
    expect(isApiError(failure)).toBe(false)
  })
})

describe('the paginated envelope', () => {
  const page = {
    items: [{ id: 'a1' }, { id: 'a2' }],
    total: 57,
    page: 2,
    pageSize: 2,
    hasMore: true,
  }

  it('comes back whole — total is every matching row, not the page size', async () => {
    stubFetch(jsonResponse(200, page))

    const result = await fetchActivity('paper', { page: 2, pageSize: 2 })

    expect(result.items).toHaveLength(2)
    expect(result.total).toBe(57)
    expect(result.page).toBe(2)
    expect(result.hasMore).toBe(true)
  })

  it('spells page size as the server aliases it, and pages from zero', async () => {
    const fetchMock = stubFetch(jsonResponse(200, page))

    await fetchActivity('paper', { page: 0, pageSize: 15 })

    expect(calledUrl(fetchMock)).toBe('/api/activity?account=paper&page=0&pageSize=15')
  })

  it('omits what the caller omitted, so the server default stays the only one', async () => {
    const fetchMock = stubFetch(jsonResponse(200, page))

    await fetchAuditLog()

    expect(calledUrl(fetchMock)).toBe('/api/settings/audit')
  })

  it('combines search and status rather than replacing one with the other', async () => {
    const fetchMock = stubFetch(jsonResponse(200, page))

    await fetchActivity('cash', { search: 'AAPL', status: 'rejected' })

    expect(calledUrl(fetchMock)).toBe(
      '/api/activity?account=cash&search=AAPL&status=rejected',
    )
  })

  it('sends the transfers page size under the same alias', async () => {
    const fetchMock = stubFetch(jsonResponse(200, page))

    await fetchTransfers('paper', { page: 1, pageSize: 50 })

    expect(calledUrl(fetchMock)).toBe(
      '/api/account/transfers?account=paper&page=1&pageSize=50',
    )
  })
})

describe('query parameters FastAPI spells its own way', () => {
  it('sends the chain window as type, max_dte and moneyness_pct', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchChain('NVDA', { type: 'put', maxDte: 30, moneynessPct: 10 })

    expect(calledUrl(fetchMock)).toBe(
      '/api/markets/chain/NVDA?type=put&max_dte=30&moneyness_pct=10',
    )
  })

  it('sends an expiry as a bare date, which both sides read as UTC', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchChain('AAPL', { expiration: '2026-11-21' })

    expect(calledUrl(fetchMock)).toBe('/api/markets/chain/AAPL?expiration=2026-11-21')
  })

  it('sends history_days in snake case', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchUnderlyings(['NVDA', 'AAPL'], 90)

    expect(calledUrl(fetchMock)).toBe(
      '/api/markets/underlyings?symbols=NVDA%2CAAPL&history_days=90',
    )
  })

  it('omits an empty symbol list rather than sending an empty filter', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchStocks([])

    expect(calledUrl(fetchMock)).toBe('/api/markets/stocks')
  })

  it('escapes the underlying in the path', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchChain('BRK/B')

    expect(calledUrl(fetchMock)).toBe('/api/markets/chain/BRK%2FB')
  })

  it('carries the history window, with A for a year', async () => {
    const fetchMock = stubFetch(jsonResponse(200, {}))

    await fetchAccountHistory('paper', { period: '1A', timeframe: '1D' })

    expect(calledUrl(fetchMock)).toBe(
      '/api/account/history?account=paper&period=1A&timeframe=1D',
    )
  })
})

describe('writing a risk ceiling', () => {
  it('sends money as a string — a JSON float is refused, on purpose', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await updateRiskLimits([{ key: 'max_risk_per_trade_pct', value: 7.5 }])

    const init = calledInit(fetchMock)
    expect(init.method).toBe('PUT')
    expect(String(init.body)).toContain('"value":"7.5"')
    expect(String(init.body)).not.toContain('"value":7.5')
  })

  it('spells a whole ceiling as a string too, so there is one shape', () => {
    expect(wireMoney(7)).toBe('7')
    expect(wireMoney(7.5)).toBe('7.5')
    expect(wireMoney(0.5)).toBe('0.5')
  })

  it('wraps the batch, because the server applies all or none', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await updateRiskLimits([
      { key: 'max_daily_loss_pct', value: 20 },
      { key: 'max_concurrent_positions', value: 8 },
    ])

    expect(JSON.parse(String(calledInit(fetchMock).body))).toEqual({
      limits: [
        { key: 'max_daily_loss_pct', value: '20' },
        { key: 'max_concurrent_positions', value: '8' },
      ],
    })
  })

  it('declares JSON on a write and asks for it on a read', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await updateRiskLimits([{ key: 'max_net_directional_pct', value: 40 }])
    await fetchPositions('paper')

    expect(calledInit(fetchMock, 0).headers).toMatchObject({
      'Content-Type': 'application/json',
    })
    expect(calledInit(fetchMock, 1).body).toBeUndefined()
  })
})

describe('the other settings writes', () => {
  it('wraps feeds under their own key', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await updateDataFeeds([{ key: 'options', value: 'indicative' }])

    expect(JSON.parse(String(calledInit(fetchMock).body))).toEqual({
      feeds: [{ key: 'options', value: 'indicative' }],
    })
  })

  it('sends a routing cell as both channels, never as one flag', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await updateNotificationRoutes([
      { event: 'stop_loss_hit', bell: true, discord: false },
    ])

    expect(JSON.parse(String(calledInit(fetchMock).body))).toEqual({
      routes: [{ event: 'stop_loss_hit', bell: true, discord: false }],
    })
  })
})

describe('halt and resume', () => {
  const halted = { halted: true, haltedReason: 'lost the feed', haltedAt: null, t0: null }

  it('halts with a stated reason', async () => {
    const fetchMock = stubFetch(jsonResponse(200, halted))

    const state = await haltEngine('lost the feed')

    expect(calledUrl(fetchMock)).toBe('/api/engine/halt')
    expect(calledInit(fetchMock).method).toBe('POST')
    expect(JSON.parse(String(calledInit(fetchMock).body))).toEqual({ reason: 'lost the feed' })
    expect(state.halted).toBe(true)
  })

  it('resumes with no body — the confirmation is the human, not a token', async () => {
    const fetchMock = stubFetch(
      jsonResponse(200, { halted: false, haltedReason: null, haltedAt: null, t0: null }),
    )

    const state = await resumeEngine()

    expect(calledUrl(fetchMock)).toBe('/api/engine/resume')
    expect(calledInit(fetchMock).method).toBe('POST')
    expect(calledInit(fetchMock).body).toBeUndefined()
    expect(state.halted).toBe(false)
  })
})
