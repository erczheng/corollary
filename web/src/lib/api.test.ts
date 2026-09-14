import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ACCOUNT_UNAVAILABLE,
  API_BASE,
  ApiError,
  HTTP_ERROR,
  MAX_SERIES_PERIOD_DAYS,
  NETWORK_UNREACHABLE,
  apiUrl,
  formatSeriesKey,
  isInvalidSeriesWindow,
  latestSession,
  ordinalSeries,
  ordinalTicks,
  quoteSeries,
  resolutionForTimeframe,
  seriesChange,
  seriesPointLabel,
  seriesSpansDays,
  sessionBoundaries,
  windowForRange,
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
  type ChartSeries,
  type SeriesPoint,
} from './api'
import type { AccountMode, UnderlyingQuote } from './types'
import { marketToday } from './format'

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

  it('asks for the window the range control resolved to', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchUnderlyings(['NVDA', 'AAPL'], { period: '1D', timeframe: '5Min' })

    expect(calledUrl(fetchMock)).toBe(
      '/api/markets/underlyings?symbols=NVDA%2CAAPL&period=1D&timeframe=5Min',
    )
  })

  it('never sends history_days, which is a deprecated alias for period', async () => {
    // Sending both is a 422. One spelling on this side is what makes that
    // unreachable rather than merely unlikely.
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchUnderlyings(['NVDA'], { period: '400D', timeframe: '1D' })

    expect(calledUrl(fetchMock)).not.toContain('history_days')
  })

  it('omits the window entirely when none is given, leaving the server its defaults', async () => {
    const fetchMock = stubFetch(jsonResponse(200, []))

    await fetchUnderlyings(['NVDA'])

    expect(calledUrl(fetchMock)).toBe('/api/markets/underlyings?symbols=NVDA')
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

/* -------------------------------------------------------------------------
 * Series — the window a range asks for, and the answer it got
 * ---------------------------------------------------------------------- */

function quoteWith(parts: Partial<UnderlyingQuote>): UnderlyingQuote {
  return {
    symbol: 'AAPL',
    price: 232.1,
    previousClose: 230.0,
    change: 2.1,
    changePct: 0.91,
    history: [],
    intraday: [],
    ...parts,
  }
}

describe('windowForRange', () => {
  const at = new Date('2026-09-13T16:00:00Z') // noon ET, a Sunday

  it('asks for the day at five-minute bars, not one daily close', () => {
    // The whole defect: 1D over daily closes is a single point.
    expect(windowForRange('1D', at)).toEqual({ period: '1D', timeframe: '5Min' })
  })

  it('asks for the week finer than hourly', () => {
    expect(windowForRange('1W', at)).toEqual({ period: '1W', timeframe: '15Min' })
  })

  it('spells a year A, not Y', () => {
    expect(windowForRange('1Y', at).period).toBe('1A')
  })

  it('takes All to the deepest window the server serves', () => {
    expect(windowForRange('All', at)).toEqual({
      period: `${MAX_SERIES_PERIOD_DAYS}D`,
      timeframe: '1D',
    })
  })

  it('computes YTD rather than reusing the one-year window', () => {
    // Jan 1 through Sep 13 inclusive is 256 days in 2026. Mapped onto `1A`
    // instead, YTD and 1Y would be the same request, the same cache entry
    // and the same chart — the defect that made 3M, YTD, 1Y and All
    // identical in the first place.
    expect(windowForRange('YTD', at)).toEqual({ period: '256D', timeframe: '1D' })
    expect(windowForRange('YTD', at)).not.toEqual(windowForRange('1Y', at))
  })

  it('measures YTD from the market day, not the UTC one', () => {
    // 04:00 UTC on Jan 1 is 23:00 ET on Dec 31 — still last year in the
    // only time zone this terminal counts days in.
    expect(windowForRange('YTD', new Date('2026-01-01T04:00:00Z')).period).toBe('365D')
    expect(windowForRange('YTD', new Date('2026-01-01T14:00:00Z')).period).toBe('1D')
  })

  it('gives every range its own window, so no two draw the same chart', () => {
    const windows = (['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All'] as const).map((r) =>
      JSON.stringify(windowForRange(r, at)),
    )

    expect(new Set(windows).size).toBe(windows.length)
  })

  it('only ever produces a period the server’s grammar accepts', () => {
    for (const range of ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All'] as const) {
      expect(windowForRange(range, at).period).toMatch(/^[1-9][0-9]{0,2}[DWMA]$/)
    }
  })
})

describe('quoteSeries', () => {
  it('reads the resolution off the response rather than the request', () => {
    const series = quoteSeries(
      quoteWith({
        intraday: [
          { at: '2026-09-11T13:30:00Z', value: 231.0 },
          { at: '2026-09-11T13:35:00Z', value: 231.4 },
        ],
      }),
    )

    expect(series.resolution).toBe('intraday')
    expect(series.points).toEqual([
      { key: '2026-09-11T13:30:00Z', value: 231.0 },
      { key: '2026-09-11T13:35:00Z', value: 231.4 },
    ])
  })

  it('keeps a daily key as the bare date it arrived as', () => {
    const series = quoteSeries(
      quoteWith({
        history: [
          { date: '2026-09-10', value: 229.5 },
          { date: '2026-09-11', value: 231.0 },
        ],
      }),
    )

    expect(series.resolution).toBe('daily')
    expect(series.points.map((p) => p.key)).toEqual(['2026-09-10', '2026-09-11'])
  })

  it('states no resolution when the window held no session', () => {
    // 1D asked on a Sunday. Both fields are present and empty, which is the
    // server *stating* there was nothing to draw — not a flat line at the
    // last price, and not a shape this client failed to read.
    expect(quoteSeries(quoteWith({}))).toEqual({
      resolution: null,
      points: [],
      missingField: false,
    })
  })

  /** A server that predates the two-field split answers with `history` and
   * no `intraday` key at all. The field is typed `IntradayPoint[]`, so
   * `quote.intraday.length` threw a TypeError mid-render and took the
   * Markets page down with it. Only the running server can be wrong about
   * this, so the client reads both fields defensively. */
  describe('a response missing a series field entirely', () => {
    it('reads the field that did arrive instead of throwing', () => {
      const skewed = { history: [{ date: '2026-09-11', value: 231.0 }], intraday: undefined }

      const series = quoteSeries(quoteWith(skewed as unknown as Partial<UnderlyingQuote>))

      expect(series.resolution).toBe('daily')
      expect(series.points).toEqual([{ key: '2026-09-11', value: 231.0 }])
      // **And still reports the missing field.** This used to assert the
      // opposite — `not.toBe(true)`, on the reasoning that a series which
      // resolved is readable by construction — and that pinned the hole:
      // this is the payload the live skew actually produces, and the flag
      // was the only thing that could see it on a daily range. The
      // resolution check cannot: `3M` asks for `1D` bars and this *is*
      // daily, so it matches while measuring the server's default window.
      //
      // Readable and complete are two questions. The series is readable,
      // which is why `points` is drawn; the response is incomplete, which
      // is why nothing that describes the window may be printed over it.
      expect(series.missingField).toBe(true)
    })

    it('separates a response it cannot read from a window with no session', () => {
      const skewed = { history: undefined, intraday: undefined }

      const series = quoteSeries(quoteWith(skewed as unknown as Partial<UnderlyingQuote>))

      // Same empty series either way, but not the same statement: this one
      // has no session *stated*, and the chart says so in those words
      // rather than reporting a quiet Sunday for a version skew.
      expect(series).toEqual({ resolution: null, points: [], missingField: true })
    })

    /** The other two half-present shapes, and the ones that actually
     * happen. Both series fields are non-optional lists in the schema, so
     * a *missing* field is always skew — never a server stating an empty
     * window. This was `&&`, which read "history: [] and no intraday key"
     * as a quiet Sunday: a stale server plus a symbol it has no daily bars
     * for in its default window — a recent listing, a halted name, a
     * ticker its universe predates — printed "3M covers no trading day
     * right now, try a longer range" at a server that ignores `period`,
     * so every longer range answers exactly the same thing. Advice that
     * cannot work, under a confident heading. */
    it('calls a missing intraday field skew rather than an empty window', () => {
      const skewed = { history: [], intraday: undefined }

      expect(quoteSeries(quoteWith(skewed as unknown as Partial<UnderlyingQuote>))).toEqual({
        resolution: null,
        points: [],
        missingField: true,
      })
    })

    it('calls a missing history field skew rather than an empty window', () => {
      const skewed = { history: undefined, intraday: [] }

      expect(quoteSeries(quoteWith(skewed as unknown as Partial<UnderlyingQuote>))).toEqual({
        resolution: null,
        points: [],
        missingField: true,
      })
    })

    it('reports it on a populated intraday series too, not just a daily one', () => {
      // The mirror of the live skew, so the flag cannot quietly become
      // "history was missing": either field absent is enough, whichever
      // one carried the points.
      const skewed = { history: undefined, intraday: [{ at: '2026-09-11T13:30:00Z', value: 231 }] }

      const series = quoteSeries(quoteWith(skewed as unknown as Partial<UnderlyingQuote>))

      expect(series.resolution).toBe('intraday')
      expect(series.points).toHaveLength(1)
      expect(series.missingField).toBe(true)
    })

    it('stays quiet on a response that carried both fields', () => {
      // The flag has to be false on a conforming server or it says
      // nothing — every drawn series would carry a skew alert. A quote
      // read off a current API states both fields, one of them empty.
      const series = quoteSeries(quoteWith({ history: [{ date: '2026-09-11', value: 231 }] }))

      expect(series.resolution).toBe('daily')
      expect(series.missingField).toBe(false)
    })
  })
})

describe('resolutionForTimeframe', () => {
  it('reads a day as daily, in either spelling', () => {
    expect(resolutionForTimeframe('1D')).toBe('daily')
    expect(resolutionForTimeframe('1Day')).toBe('daily')
  })

  it('reads everything finer as intraday', () => {
    for (const tf of ['1Min', '5Min', '15Min', '1H', '1Hour']) {
      expect(resolutionForTimeframe(tf)).toBe('intraday')
    }
  })
})

describe('formatSeriesKey', () => {
  it('formats a daily key in UTC, so a date does not render a day early', () => {
    expect(formatSeriesKey('daily', '2026-11-21')).toBe('Nov 21, 2026')
  })

  it('formats an intraday key in ET, because an instant carries its own offset', () => {
    // 13:30Z on a September day is 9:30 ET — the opening bar.
    expect(formatSeriesKey('intraday', '2026-09-11T13:30:00Z')).toBe('Sep 11, 9:30 AM')
    expect(formatSeriesKey('intraday', '2026-09-11T13:30:00Z', { compact: true })).toBe('9:30 AM')
  })

  /** Tested here rather than through a render **because a render cannot
   * see it**: `ResponsiveContainer` measures zero in jsdom, so Recharts
   * draws no axis and no `tickFormatter` ever runs. A guard the suite
   * cannot execute is a guard nobody knows is broken — and this one was
   * absent while its neighbour in `latestSession` was present, so the
   * unparseable key that no longer took the caption down still took the
   * axis down, one line later and for the same reason.
   *
   * The threat model is the same one the rest of this module is written
   * against: only the server can be wrong about the shape. A conforming
   * one serialises `date`/`datetime` through pydantic and cannot emit
   * these at all. */
  describe('a key Intl cannot parse', () => {
    it('returns the key rather than raising RangeError inside the axis', () => {
      // `Intl.DateTimeFormat#format` throws `RangeError: Invalid time
      // value` on an invalid Date, and this runs in Recharts'
      // `tickFormatter` — during render, which unmounts the chart and the
      // Markets tree around it for an axis label.
      expect(() => formatSeriesKey('daily', '2026-13-45')).not.toThrow()
      expect(formatSeriesKey('daily', '2026-13-45')).toBe('2026-13-45')
      expect(formatSeriesKey('daily', 'not a date')).toBe('not a date')
    })

    it('does the same for an intraday key, compact or not', () => {
      // `labelFormatter`, the tooltip's side of the same path.
      expect(formatSeriesKey('intraday', '2026-09-11 13:30 ET')).toBe('2026-09-11 13:30 ET')
      expect(formatSeriesKey('intraday', 'nonsense', { compact: true })).toBe('nonsense')
    })

    it('still formats every key a conforming server sends', () => {
      // The guard has to be invisible on good input or it is a second
      // bug: a fallback that swallowed real keys would print raw ISO on
      // every tick of every chart.
      expect(formatSeriesKey('daily', '2026-09-11')).toBe('Sep 11, 2026')
      expect(formatSeriesKey('intraday', '2026-09-11T13:30:00Z')).toBe('Sep 11, 9:30 AM')
    })
  })
})

describe('seriesSpansDays', () => {
  const day = (at: string) => ({ key: at, value: 1 })

  it('is false inside one session, where a bare clock time is unambiguous', () => {
    expect(seriesSpansDays([day('2026-09-11T13:30:00Z'), day('2026-09-11T20:00:00Z')])).toBe(false)
  })

  it('is true across sessions, where it would not be', () => {
    expect(seriesSpansDays([day('2026-09-10T13:30:00Z'), day('2026-09-11T20:00:00Z')])).toBe(true)
  })

  it('measures the ET day, not the UTC one', () => {
    // 00:30Z on the 11th is 20:30 ET on the 10th: one ET evening, two UTC
    // dates.
    expect(seriesSpansDays([day('2026-09-10T22:00:00Z'), day('2026-09-11T00:30:00Z')])).toBe(false)
  })

  /** The component-body half of the same fail-closed rule. This one is
   * not inside a formatter, so it throws even in jsdom — it just never
   * runs there, because every fixture parses. */
  it('fails closed as true on a key it cannot compare, rather than throwing', () => {
    // `formatDateET` raises RangeError on an invalid Date. Unknown span
    // resolves to `true`, which is the direction that keeps the date on
    // every tick: a labelled day is never ambiguous, and a bare
    // `3:45 PM` appearing four times is.
    expect(() => seriesSpansDays([day('nonsense'), day('2026-09-11T20:00:00Z')])).not.toThrow()
    expect(seriesSpansDays([day('nonsense'), day('2026-09-11T20:00:00Z')])).toBe(true)
    expect(seriesSpansDays([day('2026-09-11T13:30:00Z'), day('nonsense')])).toBe(true)
  })
})

/** The chart shows the last market day on a Saturday, and has to say so.
 *
 * The question is answered by the series — is its newest point today's ET
 * date — and never by a market calendar, which would be a second answer
 * able to disagree with the data on screen. */
describe('latestSession', () => {
  const daily = (...dates: string[]): ChartSeries => ({
    resolution: 'daily',
    points: dates.map((date) => ({ key: date, value: 1 })),
  })
  const intraday = (...instants: string[]): ChartSeries => ({
    resolution: 'intraday',
    points: instants.map((at) => ({ key: at, value: 1 })),
  })

  it('names the day and leads with it when the newest point is not today', () => {
    expect(latestSession(daily('2026-09-10', '2026-09-11'), '2026-09-12')).toEqual({
      date: '2026-09-11',
      isToday: false,
      lead: 'Last market day',
    })
  })

  it('says nothing about a last market day while the session on screen is today’s', () => {
    // A session in progress is not the last market day, and the chart
    // already reads as today because it is today.
    expect(latestSession(daily('2026-09-10', '2026-09-11'), '2026-09-11')).toEqual({
      date: '2026-09-11',
      isToday: true,
      lead: null,
    })
  })

  it('reads the newest point, not the first', () => {
    expect(latestSession(daily('2026-08-03', '2026-09-11'), '2026-09-12')?.date).toBe('2026-09-11')
  })

  it('takes a daily key as the calendar date it already is', () => {
    // The date-only trap: run through an ET formatter, 2026-09-11 becomes
    // Sep 10 and a Friday session gets labelled Thursday.
    expect(latestSession(daily('2026-09-11'), '2026-09-12')?.date).toBe('2026-09-11')
  })

  it('resolves an intraday instant to the ET day it fell on, not its UTC one', () => {
    // 00:05Z on the 12th is 8:05 PM ET on the 11th — one ET evening, two
    // UTC dates. Sliced off the string, this would claim Saturday the 12th
    // was a market day and, on the 12th, that the chart was live.
    expect(latestSession(intraday('2026-09-12T00:05:00Z'), '2026-09-12')).toEqual({
      date: '2026-09-11',
      isToday: false,
      lead: 'Last market day',
    })
  })

  it('recognises an intraday session running today', () => {
    expect(latestSession(intraday('2026-09-11T13:30:00Z', '2026-09-11T19:55:00Z'), '2026-09-11'))
      .toEqual({ date: '2026-09-11', isToday: true, lead: null })
  })

  it('defaults to today in market time, never the browser’s day', () => {
    // No second argument: the default has to be New York's date. Built from
    // `marketToday` on both sides so the assertion holds at any hour.
    expect(latestSession(daily(marketToday()))?.lead).toBeNull()
  })

  it('has no session to name when neither series field came back', () => {
    // `1D` on a Sunday. That state has its own wording; this must not add
    // a day label to it.
    expect(latestSession({ resolution: null, points: [] })).toBeNull()
  })

  it('costs the label rather than the chart when a key is unparseable', () => {
    // Intl throws RangeError on an invalid Date, and this sits above a
    // drawn chart.
    expect(latestSession(intraday('not-an-instant'), '2026-09-12')).toBeNull()
  })

  it('costs the label rather than the chart when a daily key is not a date', () => {
    // The daily branch takes the same rule as the intraday one, and did
    // not: `formatSessionDay` runs the date through Intl, so a key that is
    // not a date threw RangeError during render and blanked the page under
    // it. A fixture generating `2026-09-60` is what surfaced it; a server
    // is the only thing that could send one for real.
    expect(latestSession(daily('2026-09-60'), '2026-09-12')).toBeNull()
    expect(latestSession(daily('not-a-day'), '2026-09-12')).toBeNull()
  })
})

describe('seriesChange', () => {
  const points = [
    { key: '2026-09-11T13:30:00Z', value: 100 },
    { key: '2026-09-11T13:35:00Z', value: 110 },
    { key: '2026-09-11T13:40:00Z', value: 90 },
  ]

  it('measures from the earlier index whichever way the drag went', () => {
    expect(seriesChange(points, 0, 2)).toEqual(seriesChange(points, 2, 0))
    expect(seriesChange(points, 0, 2)?.change).toBe(-10)
    expect(seriesChange(points, 0, 2)?.changePct).toBeCloseTo(-10, 10)
  })

  it('is not a window when it never left its starting point', () => {
    expect(seriesChange(points, 1, 1)).toBeNull()
  })

  it('withholds the reading rather than dividing by a zero start', () => {
    expect(seriesChange([{ key: 'a', value: 0 }, { key: 'b', value: 5 }], 0, 1)).toBeNull()
  })

  it('withholds it for an index that is not in the series', () => {
    expect(seriesChange(points, 0, 99)).toBeNull()
    expect(seriesChange([], 0, 1)).toBeNull()
  })
})

describe('isInvalidSeriesWindow', () => {
  it('recognises the refusal a window past the point ceiling gets', () => {
    const refused = new ApiError({
      status: 422,
      code: 'invalid_series_window',
      message: 'Ask for 1W at 5Min instead, or shorten the period.',
      url: '/api/markets/underlyings',
    })

    expect(isInvalidSeriesWindow(refused)).toBe(true)
    // The message is the server's and is rendered verbatim: it names the
    // finest timeframe that would have fit.
    expect(refused.message).toContain('1W at 5Min')
  })

  it('is false for every other failure', () => {
    expect(isInvalidSeriesWindow(new Error('nope'))).toBe(false)
    expect(
      isInvalidSeriesWindow(
        new ApiError({ status: 500, code: HTTP_ERROR, message: 'boom', url: '/api' }),
      ),
    ).toBe(false)
  })
})

/* -------------------------------------------------------------------------
 * The ordinal intraday axis
 *
 * The server serves regular trading hours only, so an intraday window of
 * several sessions puts Friday 16:00 directly beside Monday 09:30. **That
 * was already true of the axis these helpers replaced** — a bare
 * `dataKey="key"` is a Recharts category on a point scale, evenly spaced by
 * position, sessions already concatenated. There was no overnight dead
 * space and no defect of that kind to fix.
 *
 * What a numeric axis over the index buys is the **seams and the ticks**: a
 * category axis can put a `ReferenceLine` only on top of a bar rather than
 * in the half-step between two, and takes no explicit `ticks` array. So
 * these four helpers are the addressable positions plus the two things that
 * pay for an axis that does not encode time — a tooltip that still says
 * when a point was, and a rule at every seam.
 *
 * The daily path keeps the category axis, and that is a statement about the
 * **axis** and not about the data: even spacing is what a category axis
 * does to whatever it is handed, irregular or not. Daily simply wants no
 * seams (there would be one at every point) and no tick control.
 * ---------------------------------------------------------------------- */

/** `count` five-minute bars from 09:30 ET on 2026-09-`day`, which is what a
 * regular-hours session now arrives as. A full one is 78 bars, not 192.
 * September is EDT, so 13:30Z *is* 09:30 ET. */
function session(day: number, count = 78): SeriesPoint[] {
  const open = Date.UTC(2026, 8, day, 13, 30)
  return Array.from({ length: count }, (_, i) => ({
    key: new Date(open + i * 5 * 60_000).toISOString(),
    value: 180 + i * 0.1,
  }))
}

describe('ordinalSeries', () => {
  // Fri 2026-09-11 and Mon 2026-09-14: the weekend is the widest gap the
  // axis has to swallow.
  const week = [...session(11), ...session(14)]

  it('puts the last point of one session next to the first of the next', () => {
    const rows = ordinalSeries(week)

    // Consecutive positions across the break — 77 then 78, with nothing
    // interpolated between them.
    expect(rows[77].index).toBe(77)
    expect(rows[78].index).toBe(78)
    expect(rows[78].index - rows[77].index).toBe(1)

    // While the instants either side of it are two and a half days apart:
    // Friday 15:55 to Monday 09:30. That is the hole a time scale renders,
    // and it dwarfs the five minutes between every other pair.
    const gap = new Date(rows[78].key).getTime() - new Date(rows[77].key).getTime()
    expect(gap).toBe(65 * 3_600_000 + 35 * 60_000)
    expect(new Date(rows[1].key).getTime() - new Date(rows[0].key).getTime()).toBe(300_000)
  })

  it('numbers every point, in the order the response gave them', () => {
    const rows = ordinalSeries(week)
    expect(rows).toHaveLength(156)
    expect(rows.map((r) => r.index)).toEqual(week.map((_, i) => i))
    expect(rows.map((r) => r.key)).toEqual(week.map((p) => p.key))
  })

  it('does not write the index back onto the series the readout reads', () => {
    // The same array feeds `seriesChange`, so this returns a new one
    // rather than annotating in place.
    const points = session(11, 3)
    ordinalSeries(points)
    expect(points[0]).not.toHaveProperty('index')
  })
})

describe('sessionBoundaries', () => {
  it('finds one seam per boundary — not one per point, and not none', () => {
    expect(sessionBoundaries('intraday', [...session(11), ...session(14)])).toEqual([78])
    expect(
      sessionBoundaries('intraday', [...session(10), ...session(11), ...session(14)]),
    ).toEqual([78, 156])
  })

  it('finds nothing to separate inside a single session', () => {
    // A 1D window: 78 bars, one day, no rule anywhere on it. A separator
    // here would assert a break that did not happen.
    expect(sessionBoundaries('intraday', session(11))).toEqual([])
    expect(sessionBoundaries('intraday', [])).toEqual([])
    expect(sessionBoundaries('intraday', session(11, 1))).toEqual([])
  })

  it('names the seam by the first point of the new session', () => {
    const week = [...session(11), ...session(14)]
    const [seam] = sessionBoundaries('intraday', week)
    // 13:30Z on the Monday — the open, not the Friday close before it.
    expect(week[seam].key).toBe('2026-09-14T13:30:00.000Z')
  })

  it('compares the ET day, not the UTC one', () => {
    // Both of these are Friday evening in New York — 7:30 PM and 9:00 PM
    // ET — and they straddle UTC midnight. A UTC-date comparison finds a
    // session break in the middle of one ET day.
    const evening: SeriesPoint[] = [
      { key: '2026-09-11T23:30:00Z', value: 180 },
      { key: '2026-09-12T01:00:00Z', value: 181 },
    ]
    expect(sessionBoundaries('intraday', evening)).toEqual([])
  })

  it('finds no seam on a key it cannot parse, rather than inventing one', () => {
    // The opposite fail-closed direction from `seriesSpansDays`, because
    // the consequence is opposite: there, not knowing costs a tick its
    // date, which is only ever more information. Here it would draw a rule
    // claiming a session break nobody can confirm.
    const broken: SeriesPoint[] = [
      { key: '2026-09-11T13:30:00Z', value: 180 },
      { key: 'not-a-time', value: 181 },
      { key: '2026-09-14T13:30:00Z', value: 182 },
    ]
    expect(() => sessionBoundaries('intraday', broken)).not.toThrow()
    expect(sessionBoundaries('intraday', broken)).toEqual([])
  })

  it('answers a daily series with no seams at all, rather than one per bar', () => {
    // The constraint is in the signature because the wrong answer is
    // spectacular: a daily key is one calendar date per session, so every
    // point after the first is a boundary and the honest reading of a
    // daily series as instants is a hairline on every bar. The component
    // guards its call site too; this is what makes the guard belong to the
    // function.
    const closes: SeriesPoint[] = [
      { key: '2026-09-09', value: 180 },
      { key: '2026-09-10', value: 181 },
      { key: '2026-09-11', value: 182 },
    ]
    expect(sessionBoundaries('daily', closes)).toEqual([])
    // `null` is "the server stated no resolution", which only happens with
    // nothing to draw — and so nothing to separate either.
    expect(sessionBoundaries(null, closes)).toEqual([])
  })
})

describe('ordinalTicks', () => {
  it('ticks the session opens when there are sessions on screen', () => {
    // Two days of bars: a label at each open, sitting where its day
    // starts.
    expect(ordinalTicks(156, [78])).toEqual([0, 78])
  })

  it('spreads ticks across a single session, ends included', () => {
    const ticks = ordinalTicks(78, [])
    expect(ticks[0]).toBe(0)
    expect(ticks[ticks.length - 1]).toBe(77)
    expect(ticks).toHaveLength(6)
    // Strictly increasing, so no two ticks land on one bar.
    expect([...ticks].sort((a, b) => a - b)).toEqual(ticks)
    expect(new Set(ticks).size).toBe(ticks.length)
  })

  it('thins to six, keeping the first and the last session labelled', () => {
    // Eleven sessions of 78 bars — more opens than the axis can hold.
    const seams = Array.from({ length: 10 }, (_, i) => (i + 1) * 78)
    const ticks = ordinalTicks(858, seams)

    expect(ticks.length).toBeLessThanOrEqual(6)
    expect(ticks[0]).toBe(0)
    // Every other day, not the first six days and then nothing: the tail
    // of the window is the part a trader is looking at.
    expect(ticks).toContain(780)
  })

  it('labels the newest session at every session count, not just the tidy ones', () => {
    // This is the property the comment above used to assert while the code
    // did not hold it. Striding from the front by `ceil(n / max)` labelled
    // the last open only when `max` divided the opens: at **eight** the
    // stride was 2, ticks landed on opens 0, 2, 4 and 6, and open 7 — the
    // newest day on screen — went unlabelled while the oldest kept a
    // label. No live window reaches eight seams (1D has none, 1W has at
    // most four), so this is held for the next range button rather than
    // for anything drawn today.
    for (let sessions = 2; sessions <= 12; sessions += 1) {
      const seams = Array.from({ length: sessions - 1 }, (_, i) => (i + 1) * 78)
      const ticks = ordinalTicks(sessions * 78, seams)

      expect(ticks.length).toBeLessThanOrEqual(6)
      expect(ticks[0]).toBe(0)
      expect(ticks[ticks.length - 1]).toBe(seams[seams.length - 1])
      // Every tick is a session open, and no two land on one bar.
      expect(ticks.every((t) => t === 0 || seams.includes(t))).toBe(true)
      expect(new Set(ticks).size).toBe(ticks.length)
      expect([...ticks].sort((a, b) => a - b)).toEqual(ticks)
    }
  })

  it('has nothing to tick on an empty series, and one tick on one point', () => {
    expect(ordinalTicks(0)).toEqual([])
    expect(ordinalTicks(1)).toEqual([0])
  })
})

describe('seriesPointLabel', () => {
  const bar = '2026-09-11T19:55:00Z'

  it('carries the date *and* the time for an intraday point', () => {
    const label = seriesPointLabel('intraday', bar)
    expect(label).toContain('Fri, Sep 11, 2026')
    expect(label).toContain('3:55 PM')
    expect(label).toContain('ET')
  })

  it('is more than the tick below it, which may be a bare clock time', () => {
    // The whole reason this exists as its own function. Once the axis is
    // ordinal, x is a position, and a tooltip reading "3:55 PM" cannot say
    // which of a week's five sessions it belongs to.
    expect(formatSeriesKey('intraday', bar, { compact: true })).toBe('3:55 PM')
    expect(seriesPointLabel('intraday', bar)).not.toBe('3:55 PM')
  })

  it('keeps the daily rule, which is the opposite one', () => {
    // A daily key is a calendar date and formats in UTC, or it renders the
    // previous day. Untouched by the ordinal change — the daily path still
    // draws on the category axis it always did.
    expect(seriesPointLabel('daily', '2026-09-11')).toBe('Sep 11, 2026')
    expect(seriesPointLabel('daily', '2026-09-11')).not.toContain('Sep 10')
  })

  it('fails closed on a key Intl cannot parse rather than throwing', () => {
    // This runs inside Recharts' `labelFormatter`: a RangeError here is a
    // blank Markets page, not a bad label.
    expect(seriesPointLabel('intraday', 'not-a-time')).toBe('not-a-time')
    expect(seriesPointLabel('daily', '2026-09-60')).toBe('2026-09-60')
  })
})
