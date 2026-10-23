import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClientProvider } from '@tanstack/react-query'
import App from '../App'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { useCashSessionReconcile } from './useCashSessionReconcile'
import {
  CASH_SESSION_CHECK_CASH_MS,
  CASH_SESSION_CHECK_PAPER_MS,
  CASH_SESSION_DROPPED_MESSAGE,
  CASH_SESSION_RETRY_BASE_MS,
  reconcileCashSessionAtBoot,
  resetCashSessionChecks,
} from '../lib/cashSession'
import { SOCKET_OPEN, startLiveSocket, stopLiveSocket, type SocketLike } from '../lib/liveSocket'
import { useUIStore } from '../lib/store'
import { queryClient } from '../lib/queryClient'
import type { CashSessionState, Notification } from '../lib/types'

const CONFIRMED_AT = '2026-10-23T14:00:00Z'

const ACTIVE: CashSessionState = {
  active: true,
  accountNumberMasked: '••••1234',
  confirmedAt: CONFIRMED_AT,
  balance: '21337.12',
}

const INACTIVE: CashSessionState = {
  active: false,
  accountNumberMasked: null,
  confirmedAt: null,
  balance: null,
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status })
}

/** The engine's session, as a variable the test moves — a restart is this
 * going inactive between two reads. */
function stubFetch(initial: CashSessionState | (() => Promise<Response> | Response), del?: () => Response) {
  let session = initial
  const calls: { method: string; url: string }[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      calls.push({ method, url })
      if (url.includes('/account/cash-session')) {
        if (method === 'DELETE')
          return Promise.resolve(del ? del() : new Response(null, { status: 204 }))
        if (method === 'POST') return Promise.reject(new Error('no check may POST'))
        return Promise.resolve(typeof session === 'function' ? session() : json(200, session))
      }
      return Promise.reject(new TypeError('fetch failed'))
    }),
  )
  return {
    set: (next: CashSessionState) => {
      session = next
    },
    gets: () => calls.filter((c) => c.method === 'GET' && c.url.includes('/cash-session')),
    deletes: () => calls.filter((c) => c.method === 'DELETE'),
    posts: () => calls.filter((c) => c.method === 'POST'),
  }
}

/** Fake the timers the watch uses and nothing else: `setImmediate` stays
 * real so `Response` bodies still read. */
function fakeTimers() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
}

/** Node's `setImmediate`, unfaked; the web tsconfig carries no Node types. */
const realImmediate = (globalThis as unknown as { setImmediate: (fn: () => void) => void }).setImmediate

async function flush() {
  for (let i = 0; i < 5; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
      await new Promise<void>((r) => realImmediate(r))
    })
  }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
  await flush()
}

/** This tab confirmed a Cash session at `CONFIRMED_AT`. */
function inCash() {
  useUIStore.setState({ accountMode: 'cash', cashSessionConfirmedAt: CONFIRMED_AT })
}

function endedNotification(id: string): Notification {
  return {
    id,
    time: '2026-10-23T15:00:00Z',
    event: 'cash_session_ended',
    severity: 'info',
    title: 'Cash session ended',
    detail: 'The Cash session for ••••1234 ended.',
    read: false,
    account: 'cash',
    correlationId: `corr-${id}`,
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
  stopLiveSocket()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/* -------------------------------------------------------------------------
 * At boot — rule 5 across a reload
 * ---------------------------------------------------------------------- */

describe('useCashSessionReconcile at boot', () => {
  it('ends a session the engine still holds, and stays in Paper', async () => {
    const net = stubFetch(ACTIVE)
    renderHook(() => useCashSessionReconcile())

    await waitFor(() => expect(net.deletes()).toHaveLength(1))
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('sends no DELETE when the engine holds none', async () => {
    const net = stubFetch(INACTIVE)
    await expect(reconcileCashSessionAtBoot()).resolves.toBe('none')
    expect(net.deletes()).toHaveLength(0)
  })

  it('is mounted at the app shell, once', async () => {
    const net = stubFetch(ACTIVE)
    render(<App />)

    await waitFor(() => expect(net.deletes()).toHaveLength(1))
    expect(net.gets()).toHaveLength(1)
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('reports, and stays in Paper, when the DELETE fails', async () => {
    stubFetch(ACTIVE, () => json(502, { error: { code: 'broker_unavailable', message: 'Alpaca is down.' } }))
    await expect(reconcileCashSessionAtBoot()).resolves.toBe('end_failed')

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(useUIStore.getState().cashSessionEndError).toMatch(/Alpaca is down\./)
  })

  it('does not undo a confirm the operator made before the read came back', async () => {
    const net = stubFetch(ACTIVE)
    useUIStore.setState({ cashSessionPending: true })

    await expect(reconcileCashSessionAtBoot()).resolves.toBe('skipped')
    expect(net.deletes()).toHaveLength(0)
  })

  it('changes nothing when the engine does not answer', async () => {
    const net = stubFetch(() => Promise.reject(new TypeError('fetch failed')))
    await expect(reconcileCashSessionAtBoot()).resolves.toBe('unreadable')
    expect(net.deletes()).toHaveLength(0)
    expect(useUIStore.getState().accountMode).toBe('paper')
  })
})

/* -------------------------------------------------------------------------
 * The standing check — audit item 1
 * ---------------------------------------------------------------------- */

describe('the standing check', () => {
  it('drops to Paper, with the reason in error styling, when the engine restarts under a Cash tab', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    render(
      <QueryClientProvider client={queryClient}>
        <AccountModeToggle />
      </QueryClientProvider>,
    )
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(useUIStore.getState().accountMode).toBe('cash')

    // The lifespan cleared the session on restart.
    net.set(INACTIVE)
    await advance(CASH_SESSION_CHECK_CASH_MS)

    expect(useUIStore.getState().accountMode).toBe('paper')
    const alert = screen.getByText(CASH_SESSION_DROPPED_MESSAGE)
    expect(alert).toHaveClass('text-error')
    expect(alert).not.toHaveClass('text-bearish')
    expect(net.deletes()).toHaveLength(0)
  })

  it('re-reads every 15s in Cash and every 60s in Paper', async () => {
    fakeTimers()
    const net = stubFetch(INACTIVE)
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(net.gets()).toHaveLength(1)

    await advance(CASH_SESSION_CHECK_PAPER_MS - 1)
    expect(net.gets()).toHaveLength(1)
    await advance(1)
    expect(net.gets()).toHaveLength(2)

    net.set(ACTIVE)
    act(() => inCash())
    await advance(CASH_SESSION_CHECK_CASH_MS)
    expect(net.gets()).toHaveLength(3)
    expect(useUIStore.getState().accountMode).toBe('cash')
  })

  it('drops tab A to Paper when tab B ends the session', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(useUIStore.getState().accountMode).toBe('cash')

    // Tab B boots in Paper and its reconcile DELETEs the session.
    net.set(INACTIVE)
    await advance(CASH_SESSION_CHECK_CASH_MS)

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(useUIStore.getState().cashSessionDropped).toBe(CASH_SESSION_DROPPED_MESSAGE)
  })

  it('drops tab A even when tab B has since confirmed a new session of its own', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    renderHook(() => useCashSessionReconcile())
    await flush()

    net.set({ ...ACTIVE, confirmedAt: '2026-10-23T14:05:00Z' })
    await advance(CASH_SESSION_CHECK_CASH_MS)

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(net.deletes()).toHaveLength(0)
  })

  it('re-checks on window focus and on the page becoming visible', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(net.gets()).toHaveLength(1)

    net.set(INACTIVE)
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await flush()
    expect(net.gets()).toHaveLength(2)
    expect(useUIStore.getState().accountMode).toBe('paper')

    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await flush()
    expect(net.gets()).toHaveLength(3)
  })

  it('does not re-check on the page being hidden', async () => {
    fakeTimers()
    const net = stubFetch(INACTIVE)
    renderHook(() => useCashSessionReconcile())
    await flush()

    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await flush()
    expect(net.gets()).toHaveLength(1)
  })

  it('re-checks when the live socket reconnects', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(net.gets()).toHaveLength(1)

    let socket: SocketLike & { readyState: number } = null as never
    startLiveSocket({
      url: 'ws://test/api/ws',
      create: () => {
        socket = {
          readyState: 0,
          send: () => {},
          close: () => {},
          onopen: null,
          onclose: null,
          onerror: null,
          onmessage: null,
        }
        return socket
      },
    })
    net.set(INACTIVE)
    act(() => {
      socket.readyState = SOCKET_OPEN
      socket.onopen?.({})
    })
    await flush()

    expect(net.gets()).toHaveLength(2)
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('re-checks when a cash_session_ended notification arrives, and not for one already held', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    inCash()
    queryClient.setQueryData(['notifications', 'cash'], [endedNotification('old')])
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(net.gets()).toHaveLength(1)

    // A re-poll that carries only what was already there is not news.
    act(() => {
      queryClient.setQueryData(['notifications', 'cash'], [endedNotification('old')])
    })
    await flush()
    expect(net.gets()).toHaveLength(1)

    net.set(INACTIVE)
    act(() => {
      queryClient.setQueryData(['notifications', 'cash'], [endedNotification('new'), endedNotification('old')])
    })
    await flush()
    expect(net.gets()).toHaveLength(2)
    expect(useUIStore.getState().accountMode).toBe('paper')
  })

  it('never moves Paper to Cash, whatever triggers the check', async () => {
    fakeTimers()
    const net = stubFetch(ACTIVE)
    // Every mode the store passes through, not a spy on one state object:
    // zustand replaces the object on each `set`.
    const modes: string[] = []
    const off = useUIStore.subscribe((state) => modes.push(state.accountMode))
    renderHook(() => useCashSessionReconcile())
    await flush()

    act(() => {
      window.dispatchEvent(new Event('focus'))
      queryClient.setQueryData(['notifications', 'paper'], [endedNotification('n1')])
    })
    await flush()
    await advance(CASH_SESSION_CHECK_PAPER_MS * 2)

    expect(useUIStore.getState().accountMode).toBe('paper')
    off()
    expect(modes.length).toBeGreaterThan(0)
    expect(modes).not.toContain('cash')
    expect(net.posts()).toHaveLength(0)
    // It ends the stray session instead, every time it finds one.
    expect(net.deletes().length).toBeGreaterThanOrEqual(1)
  })

  it('retries an unreadable check with backoff, capped at the interval', async () => {
    fakeTimers()
    const net = stubFetch(() => Promise.reject(new TypeError('fetch failed')))
    inCash()
    renderHook(() => useCashSessionReconcile())
    await flush()
    expect(net.gets()).toHaveLength(1)

    await advance(CASH_SESSION_RETRY_BASE_MS)
    expect(net.gets()).toHaveLength(2)
    await advance(CASH_SESSION_RETRY_BASE_MS * 2)
    expect(net.gets()).toHaveLength(3)
    await advance(CASH_SESSION_RETRY_BASE_MS * 4)
    expect(net.gets()).toHaveLength(4)
    // 16s would exceed the 15s Cash interval: the interval wins.
    await advance(CASH_SESSION_CHECK_CASH_MS)
    expect(net.gets()).toHaveLength(5)
    // An engine that does not answer is not an ended session.
    expect(useUIStore.getState().accountMode).toBe('cash')
  })

  it('stops checking when unmounted', async () => {
    fakeTimers()
    const net = stubFetch(INACTIVE)
    const { unmount } = renderHook(() => useCashSessionReconcile())
    await flush()
    unmount()

    await advance(CASH_SESSION_CHECK_PAPER_MS * 3)
    act(() => {
      window.dispatchEvent(new Event('focus'))
    })
    await flush()
    expect(net.gets()).toHaveLength(1)
  })
})
