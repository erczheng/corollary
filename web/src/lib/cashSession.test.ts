import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  CASH_SESSION_CONFIRM_TIMEOUT_MS,
  CASH_SESSION_CHECK_CASH_MS,
  CASH_SESSION_CHECK_PAPER_MS,
  CASH_SESSION_RETRY_BASE_MS,
  cashSessionRetryDelay,
  checkCashSession,
  confirmCash,
  resetCashSessionChecks,
  switchToPaper,
  unseenCashSessionEnded,
} from './cashSession'
import { useUIStore } from './store'

/** The store-level guard, without a rendered button.
 *
 * `AccountModeToggle.test.tsx` covers the double submit through the UI, but
 * there the disabled button alone catches it — React re-renders it disabled
 * between two synchronous clicks. This pins the second line: a caller with no
 * button (the palette, a keyboard handler) still cannot send two. */

const initialState = useUIStore.getState()

function hold() {
  let resolve!: (r: Response) => void
  const promise = new Promise<Response>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

beforeEach(() => {
  useUIStore.setState({ ...initialState }, true)
  resetCashSessionChecks()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('confirmCash', () => {
  it('sends one POST however many times it is called while one is in flight', async () => {
    const held = hold()
    const fetchMock = vi.fn(() => held.promise)
    vi.stubGlobal('fetch', fetchMock)

    const first = confirmCash('••••1234')
    const second = confirmCash('••••1234')

    await expect(second).resolves.toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(useUIStore.getState().accountMode).toBe('paper')

    held.resolve(
      new Response(
        JSON.stringify({
          active: true,
          accountNumberMasked: '••••1234',
          confirmedAt: '2026-10-23T14:00:00Z',
          balance: '21337.12',
        }),
        { status: 200 },
      ),
    )
    await expect(first).resolves.toMatchObject({ ok: true })
    expect(useUIStore.getState().accountMode).toBe('cash')
    expect(useUIStore.getState().cashSessionPending).toBe(false)
  })

  it('clears the pending flag after a refusal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: { code: 'cash_account_mismatch', message: 'No.' } }), {
            status: 409,
          }),
        ),
      ),
    )
    await expect(confirmCash('••••9999')).resolves.toEqual({
      ok: false,
      code: 'cash_account_mismatch',
      message: 'No.',
    })
    expect(useUIStore.getState().cashSessionPending).toBe(false)
    expect(useUIStore.getState().accountMode).toBe('paper')
  })
})

describe('switchToPaper', () => {
  it('does nothing while a confirm is in flight — no DELETE races the POST', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    useUIStore.setState({ accountMode: 'cash', cashSessionPending: true })

    await switchToPaper()

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

/* -------------------------------------------------------------------------
 * Audit item 2: a skipped check is owed a re-run
 * ---------------------------------------------------------------------- */

const ACTIVE_BODY = JSON.stringify({
  active: true,
  accountNumberMasked: '••••1234',
  confirmedAt: '2026-10-23T14:00:00Z',
  balance: '21337.12',
})

/** POSTs answer from `post`; GETs say the engine holds a session. */
function sessionServer(post: () => Promise<Response>) {
  const calls: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((_input: unknown, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      calls.push(method)
      if (method === 'POST') return post()
      if (method === 'DELETE') return Promise.resolve(new Response(null, { status: 204 }))
      return Promise.resolve(new Response(ACTIVE_BODY, { status: 200 }))
    }),
  )
  return calls
}

describe('checkCashSession around a confirm', () => {
  it('re-runs a check skipped during a confirm that was refused', async () => {
    const held = hold()
    const calls = sessionServer(() => held.promise)

    const confirm = confirmCash('••••1234')
    await expect(checkCashSession()).resolves.toBe('skipped')
    expect(calls).toEqual(['POST'])

    held.resolve(
      new Response(JSON.stringify({ error: { code: 'cash_account_mismatch', message: 'No.' } }), {
        status: 409,
      }),
    )
    await expect(confirm).resolves.toMatchObject({ ok: false })
    // The owed check reads, finds the engine's session, and ends it.
    await vi.waitFor(() => expect(calls).toEqual(['POST', 'GET', 'DELETE']))
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('does not re-run after a confirm that succeeded — that is newer than the skipped read', async () => {
    const held = hold()
    const calls = sessionServer(() => held.promise)

    const confirm = confirmCash('••••1234')
    await expect(checkCashSession()).resolves.toBe('skipped')
    held.resolve(new Response(ACTIVE_BODY, { status: 200 }))
    await expect(confirm).resolves.toMatchObject({ ok: true })

    await new Promise((r) => setTimeout(r, 10))
    expect(calls).toEqual(['POST'])
    expect(useUIStore.getState().accountMode).toBe('cash')
  })

  it('refuses a confirm while a check is in flight', async () => {
    useUIStore.setState({ cashSessionReconciling: true })
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(confirmCash('••••1234')).resolves.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('records the confirmed session, so a later check can tell it from another', async () => {
    sessionServer(() => Promise.resolve(new Response(ACTIVE_BODY, { status: 200 })))
    await confirmCash('••••1234')
    expect(useUIStore.getState().cashSessionConfirmedAt).toBe('2026-10-23T14:00:00Z')

    await switchToPaper()
    expect(useUIStore.getState().cashSessionConfirmedAt).toBeNull()
  })
})

describe('cashSessionRetryDelay', () => {
  it('is the mode interval with no failures', () => {
    expect(cashSessionRetryDelay('cash', 0)).toBe(CASH_SESSION_CHECK_CASH_MS)
    expect(cashSessionRetryDelay('paper', 0)).toBe(CASH_SESSION_CHECK_PAPER_MS)
  })

  it('doubles from the base and never exceeds the interval', () => {
    expect(cashSessionRetryDelay('paper', 1)).toBe(CASH_SESSION_RETRY_BASE_MS)
    expect(cashSessionRetryDelay('paper', 2)).toBe(CASH_SESSION_RETRY_BASE_MS * 2)
    expect(cashSessionRetryDelay('cash', 10)).toBe(CASH_SESSION_CHECK_CASH_MS)
    expect(cashSessionRetryDelay('paper', 10)).toBe(CASH_SESSION_CHECK_PAPER_MS)
  })
})

describe('unseenCashSessionEnded', () => {
  const base = {
    time: '2026-10-23T15:00:00Z',
    severity: 'info' as const,
    title: 't',
    detail: 'd',
    read: false,
    account: null,
    correlationId: 'c',
  }

  it('returns only cash_session_ended ids not yet seen', () => {
    const list = [
      { ...base, id: 'a', event: 'cash_session_ended' as const },
      { ...base, id: 'b', event: 'cash_session_ended' as const },
      { ...base, id: 'c', event: 'stop_loss_hit' as const },
    ]
    expect(unseenCashSessionEnded(list, new Set(['a']))).toEqual(['b'])
  })

  it('reads an absent or malformed cache entry as nothing', () => {
    expect(unseenCashSessionEnded(undefined, new Set())).toEqual([])
  })
})

describe('confirmCash hardening (re-audit)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('abandons a confirm that hangs: stays Paper, unlocks, and owes a check', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        // Never answers until aborted -- the hung engine.
        return new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('aborted', 'AbortError')),
          )
        })
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({ active: false, accountNumberMasked: null, confirmedAt: null, balance: null }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    const outcome = confirmCash('••••1234')
    expect(useUIStore.getState().cashSessionPending).toBe(true)
    await vi.advanceTimersByTimeAsync(CASH_SESSION_CONFIRM_TIMEOUT_MS)
    const result = await outcome

    expect(result).toMatchObject({ ok: false, code: 'cash_session_confirm_timeout' })
    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(useUIStore.getState().cashSessionPending).toBe(false)
    expect(CASH_SESSION_CONFIRM_TIMEOUT_MS).toBe(20_000)
  })

  it('does not treat an active answer without confirmedAt as a confirmation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({ active: true, accountNumberMasked: '••••1234', confirmedAt: null, balance: '1.00' }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )
    const result = await confirmCash('••••1234')
    expect(result).toMatchObject({ ok: false })
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('a Cash tab with no recorded confirmation drops rather than adopting any session', async () => {
    useUIStore.setState({ accountMode: 'cash', cashSessionConfirmedAt: null })
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              active: true,
              accountNumberMasked: '••••1234',
              confirmedAt: '2026-10-23T14:00:00Z',
              balance: '1.00',
            }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          ),
        ),
      ),
    )
    const result = await checkCashSession()
    expect(result).toBe('dropped')
    expect(useUIStore.getState().accountMode).toBe('paper')
  })
})
