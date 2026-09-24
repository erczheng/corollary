import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { NotificationBell } from './NotificationBell'
import { useUIStore } from '../lib/store'
import { NOTIFICATION_POLL_MS, queryKeys } from '../lib/queries'
import { formatTimeET } from '../lib/format'
import type { AccountMode, Notification } from '../lib/types'

/** The bell against a mocked `GET /api/notifications`.
 *
 * Rendered alone under a fresh `QueryClient` per test rather than inside
 * `<App />`: what is under test is the panel and the four requests it makes,
 * and a shared cache would carry one test's feed into the next.
 *
 * Items are in the API's own shape (`NotificationItem`, camelCase) — the
 * server scopes the list to the requested book plus `account: null`, which is
 * why the paper and cash payloads below each carry the engine event. */

const ENGINE_HALT: Notification = {
  id: 'n-engine',
  time: '2026-09-23T17:58:00Z',
  event: 'engine_error',
  severity: 'critical',
  title: 'Engine halted — Alpaca stream disconnected',
  detail: 'Trading stream closed with 406 — engine halted itself and will not auto-resume.',
  account: null,
  read: false,
  correlationId: 'corr-engine',
}

const REJECTED: Notification = {
  id: 'n-rejected',
  time: '2026-09-23T18:42:00Z',
  event: 'order_rejected',
  severity: 'critical',
  title: 'Order rejected',
  detail: 'NVDA 220C ×4 rejected — max risk per trade (7%) would be exceeded at 9.2%.',
  account: 'paper',
  read: false,
  correlationId: 'corr-rejected',
}

const STOP: Notification = {
  id: 'n-stop',
  time: '2026-09-23T18:31:00Z',
  event: 'stop_loss_hit',
  severity: 'warning',
  title: 'Stop loss hit',
  detail: 'NVDA260821C00220000 ×2 closed at $6.10 — stop loss. −$412.00.',
  account: 'paper',
  read: true,
  correlationId: 'corr-stop',
}

const FILLED: Notification = {
  id: 'n-filled',
  time: '2026-09-23T18:04:00Z',
  event: 'order_filled',
  severity: 'info',
  title: 'Order filled',
  detail: 'AAPL260821C00195000 ×2 bought to open at $4.10.',
  account: 'paper',
  read: true,
  correlationId: 'corr-filled',
}

const CASH_HALT: Notification = {
  id: 'n-cash-halt',
  time: '2026-09-23T16:40:00Z',
  event: 'daily_loss_halt',
  severity: 'critical',
  title: 'Daily loss halt',
  detail: 'Session loss reached 20% of equity — new entries halted.',
  account: 'cash',
  read: false,
  correlationId: 'corr-cash-halt',
}

const PAPER_FEED = [REJECTED, STOP, FILLED, ENGINE_HALT]
const CASH_FEED = [CASH_HALT, ENGINE_HALT]

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function pending(): Promise<Response> {
  return new Promise<never>(() => {})
}

interface Call {
  method: string
  path: string
  account: string | null
}

type Answer = Response | Promise<Response> | (() => Response | Promise<Response>)

interface Answers {
  paper?: Answer
  cash?: Answer
  /** Answer to a POST, given the id and the action. Defaults to the item
   * with `read: true` — the server's row after the write. */
  write?: (id: string, action: 'read' | 'dismiss') => Response
}

let calls: Call[] = []

/** A stateful server: a read sticks and a dismissal stays gone, so the
 * refetch that follows every write reads back what was stored — the way the
 * real endpoint behaves, and the only way a test can tell a cache write from
 * a server write. */
function stubFetch(answers: Answers = {}) {
  const byId = new Map([...PAPER_FEED, ...CASH_FEED].map((n) => [n.id, n]))
  const readIds = new Set<string>()
  const dismissedIds = new Set<string>()
  const stored = (list: Notification[]) =>
    list
      .filter((n) => !dismissedIds.has(n.id))
      .map((n) => (readIds.has(n.id) ? { ...n, read: true } : n))
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), 'http://127.0.0.1')
    const method = init?.method ?? 'GET'
    const account = url.searchParams.get('account')
    calls.push({ method, path: url.pathname, account })

    const write = url.pathname.match(/^\/api\/notifications\/([^/]+)\/(read|dismiss)$/)
    if (write && method === 'POST') {
      const id = decodeURIComponent(write[1])
      const action = write[2] as 'read' | 'dismiss'
      if (answers.write) return Promise.resolve(answers.write(id, action))
      const item = byId.get(id)
      if (!item) {
        return Promise.resolve(
          jsonResponse(404, {
            error: { code: 'notification_not_found', message: 'No such notification.' },
          }),
        )
      }
      if (action === 'read') readIds.add(id)
      else dismissedIds.add(id)
      return Promise.resolve(jsonResponse(200, { ...item, read: readIds.has(id) || item.read }))
    }
    if (url.pathname === '/api/notifications') {
      const answer =
        account === 'cash'
          ? (answers.cash ?? (() => jsonResponse(200, stored(CASH_FEED))))
          : (answers.paper ?? (() => jsonResponse(200, stored(PAPER_FEED))))
      return Promise.resolve(typeof answer === 'function' ? answer() : answer)
    }
    return Promise.resolve(jsonResponse(404, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const initialState = useUIStore.getState()
let client: QueryClient

beforeEach(() => {
  calls = []
  useUIStore.setState({ ...initialState }, true)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  client.clear()
  vi.unstubAllGlobals()
})

function renderBell(mode: AccountMode = 'paper') {
  useUIStore.setState({ accountMode: mode })
  return render(
    <QueryClientProvider client={client}>
      <NotificationBell />
    </QueryClientProvider>,
  )
}

function bell(): HTMLElement {
  return screen.getByRole('button', { name: /^Notifications/ })
}

function openPanel(): HTMLElement {
  fireEvent.click(bell())
  return screen.getByRole('dialog', { name: 'Notifications' })
}

/** Wait for the first read to land, then open. */
async function openLoaded(mode: AccountMode = 'paper'): Promise<HTMLElement> {
  renderBell(mode)
  await waitFor(() => expect(client.getQueryState(queryKeys.notifications(mode))?.status).not.toBe('pending'))
  return openPanel()
}

function writes(): Call[] {
  return calls.filter((c) => c.method === 'POST')
}

describe('the bell', () => {
  it('opens and closes the panel, and on Escape and an outside click', async () => {
    stubFetch()
    renderBell()
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()

    fireEvent.click(bell())
    expect(bell()).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(bell())
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()

    openPanel()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()

    openPanel()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
  })

  it('reads the selected book from the API', async () => {
    stubFetch()
    renderBell('cash')
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    expect(calls[0]).toEqual({ method: 'GET', path: '/api/notifications', account: 'cash' })
  })

  /** Off the socket on purpose — rule 9 halts *because* the socket died — so
   * the poll is the delivery path, and its cadence is the contract. */
  it('polls every 15 seconds', async () => {
    stubFetch()
    renderBell()
    await waitFor(() => expect(client.getQueryCache().find({ queryKey: queryKeys.notifications('paper') })).toBeDefined())
    const query = client.getQueryCache().find({ queryKey: queryKeys.notifications('paper') })!
    const observer = query.observers[0]
    expect(NOTIFICATION_POLL_MS).toBe(15_000)
    expect(observer.options.refetchInterval).toBe(15_000)
  })
})

describe('loading, error and empty are three different states', () => {
  it('shows a loading state until the first read lands', () => {
    stubFetch({ paper: pending() })
    renderBell()
    const panel = openPanel()

    expect(within(panel).getByRole('status', { name: 'Loading notifications' })).toBeInTheDocument()
    expect(within(panel).queryByText(/Nothing yet/)).not.toBeInTheDocument()
  })

  /** The one claim a notification panel must never make falsely is "nothing
   * to report" — an engine halt could be sitting behind a dead endpoint. */
  it('says an unreachable engine is unreachable, in error, not empty', async () => {
    stubFetch({ paper: () => Promise.reject(new TypeError('Failed to fetch')) })
    renderBell()
    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications — unavailable'))
    const panel = openPanel()

    const alert = within(panel).getByRole('alert')
    expect(alert).toHaveTextContent(/engine did not answer/)
    expect(alert.className).toContain('text-error')
    expect(alert.className).not.toContain('text-bearish')
    expect(within(panel).getByText(/unavailable, not empty/)).toBeInTheDocument()
    expect(within(panel).queryByText(/Nothing yet/)).not.toBeInTheDocument()
  })

  it('treats a body that is not a list as an error, not an empty bell', async () => {
    stubFetch({ paper: jsonResponse(200, {}) })
    renderBell()
    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications — unavailable'))
    const panel = openPanel()

    expect(within(panel).getByRole('alert')).toHaveTextContent(/not with a list of notifications/)
    expect(within(panel).queryByText(/Nothing yet/)).not.toBeInTheDocument()
  })

  it('explains an empty feed instead of rendering an empty list', async () => {
    stubFetch({ paper: jsonResponse(200, []) })
    const panel = await openLoaded()

    expect(within(panel).queryByRole('listitem')).not.toBeInTheDocument()
    expect(within(panel).getByText(/nothing to report/)).toBeInTheDocument()
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the list after a failed refresh and says it may be out of date', async () => {
    let fail = false
    stubFetch({
      paper: () =>
        fail ? Promise.reject(new TypeError('Failed to fetch')) : jsonResponse(200, PAPER_FEED),
    })
    const panel = await openLoaded()
    fail = true
    await act(() => client.refetchQueries({ queryKey: queryKeys.notifications('paper') }))

    await waitFor(() => expect(within(panel).getByText(/may be out of date/)).toBeInTheDocument())
    expect(within(panel).getByText(REJECTED.detail)).toBeInTheDocument()
  })
})

/** The header has no other engine-status indicator, so the closed bell is the
 * only place a dead endpoint can show without anyone opening anything. A quiet
 * glyph there reads as "nothing to report". */
describe('the closed bell', () => {
  function marker(): Element | null {
    return bell().querySelector('[data-marker="unavailable"]')
  }

  it('makes no claim about unread while the first read is pending', () => {
    stubFetch({ paper: pending() })
    renderBell()

    expect(bell()).toHaveAttribute('aria-label', 'Notifications — loading')
    expect(bell()).toHaveAttribute('title', 'Notifications — loading')
    expect(bell().getAttribute('title')).not.toMatch(/no unread/)
    expect(marker()).toBeNull()
  })

  it('carries no marker when the feed is healthy', async () => {
    stubFetch()
    renderBell()
    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications — 2 unread'))
    expect(marker()).toBeNull()
  })

  it('marks itself in error when the first read fails, without being opened', async () => {
    stubFetch({ paper: () => Promise.reject(new TypeError('Failed to fetch')) })
    renderBell()

    await waitFor(() => expect(marker()).not.toBeNull())
    expect(screen.queryByRole('dialog', { name: 'Notifications' })).not.toBeInTheDocument()
    expect(marker()!.className).toContain('text-error')
    expect(marker()!.className).toContain('border-error')
    expect(bell().className).toContain('text-error')
    expect(bell().outerHTML).not.toContain('bearish')
    expect(bell()).toHaveAttribute('aria-label', 'Notifications — unavailable')
    expect(bell().getAttribute('title')).toMatch(/unavailable/)
  })

  it('marks itself in error when a poll fails after a good read, keeping the stale count', async () => {
    let fail = false
    stubFetch({
      paper: () =>
        fail ? Promise.reject(new TypeError('Failed to fetch')) : jsonResponse(200, PAPER_FEED),
    })
    renderBell()
    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications — 2 unread'))
    expect(marker()).toBeNull()

    fail = true
    await act(() => client.refetchQueries({ queryKey: queryKeys.notifications('paper') }))

    await waitFor(() => expect(marker()).not.toBeNull())
    expect(marker()!.className).toContain('text-error')
    expect(bell()).toHaveAttribute(
      'aria-label',
      'Notifications — 2 unread as of the last good read, may be out of date',
    )
    expect(bell().getAttribute('title')).toMatch(/may be out of date/)
    // The count survives alongside the marker, as a separate element.
    const badge = [...bell().querySelectorAll('span')].find((s) => s.textContent === '2')
    expect(badge).toBeDefined()
    expect(badge).not.toBe(marker())
  })

  it('clears the marker once a poll succeeds again', async () => {
    let fail = true
    stubFetch({
      paper: () =>
        fail ? Promise.reject(new TypeError('Failed to fetch')) : jsonResponse(200, PAPER_FEED),
    })
    renderBell()
    await waitFor(() => expect(marker()).not.toBeNull())

    fail = false
    await act(() => client.refetchQueries({ queryKey: queryKeys.notifications('paper') }))

    await waitFor(() => expect(marker()).toBeNull())
    expect(bell()).toHaveAttribute('aria-label', 'Notifications — 2 unread')
  })
})

describe('the panel contents', () => {
  it('lists the feed newest first, with the engine’s titles and ET times', async () => {
    stubFetch()
    const panel = await openLoaded()

    const items = within(panel).getAllByRole('listitem')
    expect(items).toHaveLength(PAPER_FEED.length)
    PAPER_FEED.forEach((n, i) => {
      expect(within(items[i]).getByText(n.title)).toBeInTheDocument()
      expect(within(items[i]).getByText(n.detail)).toBeInTheDocument()
      expect(within(items[i]).getByText(formatTimeET(n.time))).toBeInTheDocument()
    })
  })

  /** `account: null` belongs to no book. Hiding an engine fault behind
   * whichever account happens to be selected is the failure this prevents. */
  it('shows an account-less engine event under both books', async () => {
    stubFetch()
    const paper = await openLoaded('paper')
    expect(within(paper).getByText(ENGINE_HALT.detail)).toBeInTheDocument()
    expect(within(paper).queryByText(CASH_HALT.detail)).not.toBeInTheDocument()

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    await waitFor(() => expect(within(paper).getByText(CASH_HALT.detail)).toBeInTheDocument())
    expect(within(paper).getByText(ENGINE_HALT.detail)).toBeInTheDocument()
    expect(within(paper).queryByText(REJECTED.detail)).not.toBeInTheDocument()
  })

  it('scopes on the client too — another book’s row in the response is not shown', async () => {
    stubFetch({ cash: jsonResponse(200, [REJECTED, ...CASH_FEED]) })
    const panel = await openLoaded('cash')

    expect(within(panel).queryByText(REJECTED.detail)).not.toBeInTheDocument()
    expect(within(panel).getAllByRole('listitem')).toHaveLength(CASH_FEED.length)
  })

  /** The routing gate is applied when the engine emits. Unchecking a route
   * must not retroactively erase a notification already received. */
  it('applies no routing filter of its own', async () => {
    useUIStore.setState({
      notificationRoutes: initialState.notificationRoutes.map((r) => ({ ...r, bell: false })),
    })
    stubFetch()
    const panel = await openLoaded()
    expect(within(panel).getAllByRole('listitem')).toHaveLength(PAPER_FEED.length)
  })

  /** Only real API events. A fill invented by the fixture broker must never
   * sit beside a real engine halt. */
  it('never shows the fixture store’s notifications', async () => {
    const fixture = { ...FILLED, id: 'fixture-only', detail: 'A fill the mock broker invented.' }
    useUIStore.setState({ notifications: [fixture] })
    stubFetch({ paper: jsonResponse(200, []) })
    const panel = await openLoaded()

    expect(within(panel).queryByText(fixture.detail)).not.toBeInTheDocument()
    expect(within(panel).getByText(/nothing to report/)).toBeInTheDocument()
  })
})

describe('severity', () => {
  it('renders critical in error and a stop loss as a warning in caution', async () => {
    stubFetch()
    const panel = await openLoaded()

    const rejection = within(panel).getByText(REJECTED.title)
    const stop = within(panel).getByText(STOP.title)
    expect(rejection.className).toContain('text-error')
    expect(stop.className).toContain('text-caution')
    expect(stop.className).not.toContain('text-error')
    expect(panel.innerHTML).not.toContain('text-bearish')
  })

  /** Severity is stored as raised. The client's event map must not recolour
   * a row the engine raised at a different level. */
  it('takes the server’s severity over the client’s event map', async () => {
    const loud = { ...FILLED, id: 'n-loud', title: 'Fill at an unexpected price', severity: 'critical' as const }
    stubFetch({ paper: jsonResponse(200, [loud]) })
    const panel = await openLoaded()

    expect(within(panel).getByText(loud.title).className).toContain('text-error')
  })
})

describe('unread, read and dismiss', () => {
  it('badges the unread count for the book on screen only', async () => {
    stubFetch()
    renderBell()
    // REJECTED and ENGINE_HALT are unread in paper; CASH_HALT is cash's.
    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications — 2 unread'))
    expect(bell().textContent).toBe('2')
  })

  /** Glancing at a badge is not reading eight notifications. */
  it('does not mark anything read by opening', async () => {
    stubFetch()
    await openLoaded()
    expect(writes()).toEqual([])
    expect(bell()).toHaveAttribute('aria-label', 'Notifications — 2 unread')
  })

  it('marks the visible unread read, one POST each, scoped to this book', async () => {
    stubFetch()
    const panel = await openLoaded()

    fireEvent.click(within(panel).getByRole('button', { name: 'Mark all read' }))

    await waitFor(() => expect(bell()).toHaveAttribute('aria-label', 'Notifications'))
    expect(writes()).toEqual(
      expect.arrayContaining([
        { method: 'POST', path: `/api/notifications/${REJECTED.id}/read`, account: 'paper' },
        { method: 'POST', path: `/api/notifications/${ENGINE_HALT.id}/read`, account: 'paper' },
      ]),
    )
    // Only the unread ones — and never cash's, which this book cannot see.
    expect(writes().filter((c) => c.path.endsWith('/read'))).toHaveLength(2)
    expect(writes().some((c) => c.path.includes(CASH_HALT.id))).toBe(false)
    expect(bell().textContent).toBe('')
  })

  it('dismisses one notification through its endpoint', async () => {
    stubFetch()
    const panel = await openLoaded()

    fireEvent.click(within(panel).getByRole('button', { name: 'Dismiss Order rejected' }))

    await waitFor(() => expect(within(panel).queryByText(REJECTED.detail)).not.toBeInTheDocument())
    expect(writes()).toContainEqual({
      method: 'POST',
      path: `/api/notifications/${REJECTED.id}/dismiss`,
      account: 'paper',
    })
    expect(within(panel).getAllByRole('listitem')).toHaveLength(PAPER_FEED.length - 1)
  })

  it('dismisses under the cash book with the cash scope', async () => {
    stubFetch()
    const panel = await openLoaded('cash')

    fireEvent.click(within(panel).getByRole('button', { name: 'Dismiss Daily loss halt' }))

    await waitFor(() =>
      expect(writes()).toContainEqual({
        method: 'POST',
        path: `/api/notifications/${CASH_HALT.id}/dismiss`,
        account: 'cash',
      }),
    )
  })

  it('says so when a dismiss is refused, rather than failing silently', async () => {
    stubFetch({
      write: () =>
        jsonResponse(404, {
          error: { code: 'notification_not_found', message: 'No such notification in this book.' },
        }),
    })
    const panel = await openLoaded()

    fireEvent.click(within(panel).getByRole('button', { name: 'Dismiss Order rejected' }))

    await waitFor(() =>
      expect(within(panel).getByRole('alert')).toHaveTextContent('No such notification in this book.'),
    )
    expect(within(panel).getByRole('alert').className).toContain('text-error')
  })

  /** The mutations are not keyed by account and the bell does not remount on
   * a switch — a Paper refusal must not follow you into the Cash panel. */
  it('does not carry a refused change across an account switch', async () => {
    stubFetch({
      write: () =>
        jsonResponse(404, {
          error: { code: 'notification_not_found', message: 'No such notification in this book.' },
        }),
    })
    const panel = await openLoaded('paper')

    fireEvent.click(within(panel).getByRole('button', { name: 'Dismiss Order rejected' }))
    await waitFor(() =>
      expect(within(panel).getByRole('alert')).toHaveTextContent('No such notification in this book.'),
    )

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    await waitFor(() => expect(within(panel).getByText(CASH_HALT.detail)).toBeInTheDocument())
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument()
    expect(within(panel).queryByText('No such notification in this book.')).not.toBeInTheDocument()
  })
})
