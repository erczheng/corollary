import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { SENTIMENT_AUDIT_STATES } from '../lib/mockData'
import type {
  AccountResponse,
  ApiKeyPresence,
  AuditLogEntry,
  DataFeed,
  DataSourceStatus,
  NotificationRoute,
  Page,
  RiskLimit,
  SentimentAuditStatus,
} from '../lib/types'

/** Settings reads seven endpoints and writes three of them. Every payload
 * below is what `http://127.0.0.1:5173/api/settings/*` actually answered on
 * 2026-09-12, trimmed of nothing, so a shape that drifts on the server shows
 * up here rather than in a browser.
 *
 * The account payload is the real paper book — **equity 99,901.08**, not the
 * $100,000 of fixture money this page used to size its ceilings against. The
 * dollar figure in a raise confirm is the whole reason this page reads the
 * account at all, so the tests pin the arithmetic against that number.
 */
const LIMITS: RiskLimit[] = [
  {
    key: 'max_risk_per_trade_pct',
    label: 'Max risk per trade',
    value: 7,
    unit: '%',
    min: 1,
    max: 25,
    help:
      'Ceiling on what one position may lose, as a share of account equity. Risk is maximum loss ' +
      'at expiry for a defined-risk structure, premium paid for a long option, and a ±2σ stress ' +
      'loss for anything undefined-risk.',
  },
  {
    key: 'max_daily_loss_pct',
    label: 'Max daily loss',
    value: 20,
    unit: '%',
    min: 1,
    max: 50,
    help: 'Realized + unrealized loss in one session that triggers an automatic halt.',
  },
  {
    key: 'max_concurrent_positions',
    label: 'Max concurrent positions',
    value: 8,
    unit: 'count',
    min: 1,
    max: 20,
    help: 'How many positions may be open at once, across every strategy.',
  },
  {
    key: 'max_exposure_per_underlying',
    label: 'Max exposure per underlying',
    value: 25,
    unit: '%',
    min: 5,
    max: 100,
    help: 'Ceiling on combined risk across every position sharing one underlying.',
  },
  {
    key: 'max_net_directional_pct',
    label: 'Max net directional exposure',
    value: 40,
    unit: '%',
    min: 5,
    max: 100,
    help: 'Ceiling on net long-minus-short delta exposure, as a share of equity.',
  },
]

const FEEDS: DataFeed[] = [
  {
    key: 'options',
    envVar: 'ALPACA_OPTIONS_FEED',
    label: 'Options quotes',
    value: 'indicative',
    help: 'Indicative is a 15-minute-delayed derivative of OPRA, not OPRA itself.',
  },
  {
    key: 'stockHistorical',
    envVar: 'ALPACA_STOCK_FEED_HISTORICAL',
    label: 'Equity bars (historical)',
    value: 'sip',
    help: 'SIP is 100% of US volume and is free for anything older than 15 minutes.',
  },
  {
    key: 'stockRealtime',
    envVar: 'ALPACA_STOCK_FEED_REALTIME',
    label: 'Equity quotes (real-time)',
    value: 'iex',
    help: 'Real-time SIP requires the paid plan; IEX is ~2.5% of US volume.',
  },
]

const ROUTES: NotificationRoute[] = [
  { event: 'order_filled', bell: true, discord: true },
  { event: 'order_rejected', bell: true, discord: true },
  { event: 'stop_loss_hit', bell: true, discord: true },
  { event: 'daily_loss_halt', bell: true, discord: true },
  { event: 'engine_error', bell: true, discord: true },
  { event: 'operator_halt', bell: true, discord: true },
  { event: 'operator_resume', bell: true, discord: true },
  { event: 'price_alert', bell: true, discord: true },
  { event: 'recommendations_ready', bell: true, discord: false },
  { event: 'strategy_promotion', bell: true, discord: false },
  { event: 'risk_limits_changed', bell: false, discord: true },
  { event: 'data_feeds_changed', bell: false, discord: true },
  { event: 'notification_routes_changed', bell: false, discord: true },
  { event: 'watchlist_changed', bell: false, discord: true },
]

const KEYS: ApiKeyPresence[] = [
  {
    envVar: 'ALPACA_PAPER_API_KEY',
    purpose: 'Paper execution + market data',
    present: true,
    optional: false,
  },
  {
    envVar: 'ALPACA_LIVE_API_KEY',
    purpose: 'Cash execution — absent until Phase 7',
    present: false,
    optional: true,
  },
  {
    envVar: 'ANTHROPIC_API_KEY',
    purpose: 'LLM enrichment + Research chat',
    present: true,
    optional: false,
  },
  {
    envVar: 'DISCORD_WEBHOOK_URL',
    purpose: 'Discord notification channel',
    present: true,
    optional: true,
  },
]

const SOURCES: DataSourceStatus[] = [
  {
    name: 'Alpaca (execution + data)',
    status: 'degraded',
    detail:
      'Paper account, Basic plan. Engine halted — no reason recorded. Recovery is an explicit ' +
      'resume; nothing resumes itself.',
  },
  {
    name: 'Finnhub (news + calendar)',
    status: 'connected',
    detail: 'Key present; the market cap column reads it.',
  },
  {
    name: 'FRED (macro)',
    status: 'disconnected',
    detail: 'Key present; nothing reads it yet.',
  },
]

const PAPER_EQUITY = 99901.08

const ACCOUNT = {
  account: 'paper',
  status: 'ACTIVE',
  currency: 'USD',
  cash: 53386.08,
  equity: PAPER_EQUITY,
  lastEquity: 100116.08,
  dayChange: -215,
  balanceTrend: { changePct: -0.2148, comparedTo: 'vs previous close' },
  buyingPower: 319786.72,
  optionsBuyingPower: 71215.08,
  longMarketValue: 54186,
  shortMarketValue: -7671,
  netPositionValue: 46515,
  grossPositionValue: 61857,
  derivedEquity: PAPER_EQUITY,
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
  cashAccountUnavailableReason: 'Cash trading is unavailable: live credentials are not set.',
  missingLiveCredentialEnvVars: ['ALPACA_LIVE_API_KEY', 'ALPACA_LIVE_SECRET_KEY'],
} as unknown as AccountResponse

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => headers[name] ?? null },
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function page<T>(items: T[], total = items.length): Page<T> {
  return { items, total, page: 0, pageSize: 100, hasMore: total > items.length }
}

/** The 422 the server answers a write it will not store with. Rule 8's
 * reasoning applied to config: the refusal names the rule, and the page has
 * to render it rather than reverting the control in silence. */
function refusal(message: string): Response {
  return jsonResponse(422, { error: { code: 'invalid_feed_value', message } })
}

interface Overrides {
  account?: Response
  keys?: Response
  limits?: Response
  routes?: Response
  feeds?: Response
  sources?: Response
  audit?: Response
  putLimits?: Response
  putRoutes?: Response
  putFeeds?: Response
  sentiment?: Response
  postAudit?: Response
}

/** A very small stand-in for the settings half of the engine.
 *
 * It holds state and audits its own writes, because most of what this page
 * does is a round trip: a `PUT` answers with the new list, the mutation seeds
 * the cache with it, and the audit query is invalidated. A stub that answered
 * the same payload forever would let a page that ignored the response pass.
 */
let served: {
  limits: RiskLimit[]
  feeds: DataFeed[]
  routes: NotificationRoute[]
  audit: AuditLogEntry[]
  /** What `GET /settings/sentiment` answers. Mutable so a test can close a
   * run between reads, the way the server would. */
  sentiment: SentimentAuditStatus
}

let writes: { url: string; body: unknown }[] = []

function audited(category: AuditLogEntry['category'], field: string, was: string, became: string) {
  served.audit = [
    {
      id: `audit-${served.audit.length + 1}`,
      time: '2026-09-12T14:30:00Z',
      category,
      field,
      previousValue: was,
      newValue: became,
    },
    ...served.audit,
  ]
}

function stubFetch(overrides: Overrides = {}) {
  const fetchMock = vi.fn((input: unknown, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null

    // Before every GET branch: `/settings/sentiment/audit` would otherwise
    // be answered as a read of `/settings/sentiment`.
    if (method === 'POST' && url.includes('/settings/sentiment/audit')) {
      writes.push({ url, body })
      return Promise.resolve(
        overrides.postAudit ??
          jsonResponse(202, {
            status: 'started',
            trigger: 'manual',
            runId: 8,
            requestedAt: '2026-10-14T15:20:00Z',
          }),
      )
    }

    if (method === 'PUT') {
      writes.push({ url, body })

      if (url.includes('/settings/limits')) {
        if (overrides.putLimits) return Promise.resolve(overrides.putLimits)
        for (const update of (body?.limits ?? []) as { key: string; value: string }[]) {
          const limit = served.limits.find((l) => l.key === update.key)
          if (!limit) continue
          audited('risk', update.key, String(limit.value), update.value)
          limit.value = Number(update.value)
        }
        served.limits = served.limits.map((l) => ({ ...l }))
        return Promise.resolve(jsonResponse(200, served.limits))
      }

      if (url.includes('/settings/feeds')) {
        if (overrides.putFeeds) return Promise.resolve(overrides.putFeeds)
        for (const update of (body?.feeds ?? []) as { key: string; value: string }[]) {
          const feed = served.feeds.find((f) => f.key === update.key)
          if (!feed) continue
          // Keyed by the **env var**, exactly as the server writes it.
          audited('feed', feed.envVar, feed.value, update.value)
          feed.value = update.value
        }
        served.feeds = served.feeds.map((f) => ({ ...f }))
        return Promise.resolve(jsonResponse(200, served.feeds))
      }

      if (url.includes('/settings/routes')) {
        if (overrides.putRoutes) return Promise.resolve(overrides.putRoutes)
        for (const update of (body?.routes ?? []) as NotificationRoute[]) {
          const route = served.routes.find((r) => r.event === update.event)
          if (!route) continue
          for (const channel of ['bell', 'discord'] as const) {
            if (route[channel] !== update[channel]) {
              audited(
                'notification',
                `${update.event}.${channel}`,
                route[channel] ? 'on' : 'off',
                update[channel] ? 'on' : 'off',
              )
            }
          }
          route.bell = update.bell
          route.discord = update.discord
        }
        served.routes = served.routes.map((r) => ({ ...r }))
        return Promise.resolve(jsonResponse(200, served.routes))
      }
    }

    if (url.includes('/settings/limits')) {
      return Promise.resolve(overrides.limits ?? jsonResponse(200, served.limits))
    }
    if (url.includes('/settings/feeds')) {
      return Promise.resolve(overrides.feeds ?? jsonResponse(200, served.feeds))
    }
    if (url.includes('/settings/routes')) {
      return Promise.resolve(overrides.routes ?? jsonResponse(200, served.routes))
    }
    if (url.includes('/settings/sentiment')) {
      return Promise.resolve(
        overrides.sentiment ?? jsonResponse(200, served.sentiment),
      )
    }
    if (url.includes('/settings/keys')) {
      return Promise.resolve(overrides.keys ?? jsonResponse(200, KEYS))
    }
    if (url.includes('/settings/sources')) {
      return Promise.resolve(overrides.sources ?? jsonResponse(200, SOURCES))
    }
    if (url.includes('/settings/audit')) {
      return Promise.resolve(overrides.audit ?? jsonResponse(200, page(served.audit)))
    }
    if (url.includes('/account')) {
      return Promise.resolve(overrides.account ?? jsonResponse(200, ACCOUNT))
    }
    // Nothing else on this page fetches. A 200 with an empty body keeps an
    // unexpected call from reading as the failure under test.
    return Promise.resolve(jsonResponse(200, {}))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/settings')
  // Rule 5: every start is Paper. A leaked `cash` here would be a test
  // ordering bug that reads as a data bug.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
  writes = []
  served = {
    limits: LIMITS.map((l) => ({ ...l })),
    feeds: FEEDS.map((f) => ({ ...f })),
    routes: ROUTES.map((r) => ({ ...r })),
    audit: [],
    sentiment: SENTIMENT_AUDIT_STATES.populated,
  }
})

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

/** Writes to one endpoint, as bodies. Several tests care that the *string*
 * form went out — a ceiling is stored exactly and a JSON float is refused by
 * the server outright. */
function writesTo(path: string): unknown[] {
  return writes.filter((w) => w.url.includes(path)).map((w) => w.body)
}

describe('API keys', () => {
  it('lists each variable the server reports, with its presence and nothing else', async () => {
    stubFetch()
    render(<App />)

    const keys = section('API keys')
    // Waits on a cell, not on the table: the loading skeleton *is* a table,
    // so `findByRole('table')` resolves against the skeleton and every
    // assertion after it runs against an empty grid.
    await within(keys).findByText('ALPACA_PAPER_API_KEY')
    const table = within(keys).getByRole('table')
    for (const key of KEYS) {
      expect(within(table).getByText(key.envVar)).toBeInTheDocument()
    }
    expect(within(table).getAllByText('Set')).toHaveLength(3)
    expect(within(table).getAllByText('Not set')).toHaveLength(1)
  })

  /** CLAUDE.md rule 6: the UI never renders a key. Not a masked one, not a
   * last-four — there is no element on this page that could contain key
   * material, because the endpoint answers with booleans. */
  it('renders no field that could hold key material', async () => {
    stubFetch()
    render(<App />)
    const keys = section('API keys')
    await within(keys).findByText('ALPACA_PAPER_API_KEY')

    expect(within(keys).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(keys).queryByText(/•/)).not.toBeInTheDocument()
    expect(keys.textContent ?? '').not.toMatch(/PK[A-Z0-9]{8}/)
  })

  it('says the values are never sent to the browser', async () => {
    stubFetch()
    render(<App />)
    expect(within(section('API keys')).getByText(/never displayed here/)).toBeInTheDocument()
  })

  it('renders a labelled skeleton rather than an empty table while it reads', () => {
    stubFetch()
    render(<App />)
    // The skeleton names what is loading for a screen reader, in an
    // `sr-only` span inside its `role="status"`.
    expect(screen.getByText('Reading which credentials are configured')).toBeInTheDocument()
  })

  it('renders a failed read as an error, not as an empty list', async () => {
    stubFetch({ keys: jsonResponse(403, { error: { code: 'forbidden', message: 'No.' } }) })
    render(<App />)

    const alert = await within(section('API keys')).findByRole('alert')
    expect(alert.textContent).toContain('No.')
  })
})

describe('risk limits', () => {
  /** CLAUDE.md: a percentage ceiling is uninterpretable without these three
   * definitions, so the page states them rather than leaving each reader to
   * assume one. */
  it('states what risk means for each structure type', () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    expect(within(risk).getByText(/maximum loss at expiry/)).toBeInTheDocument()
    expect(within(risk).getByText(/premium paid/)).toBeInTheDocument()
    expect(within(risk).getByText(/±2σ/)).toBeInTheDocument()
  })

  it('says the engine enforces and this page only stores', () => {
    stubFetch()
    render(<App />)
    expect(
      within(section('Risk limits')).getByText(/never trusts a value sent from this page/),
    ).toBeInTheDocument()
  })

  it('shows no Save button until the value actually changes', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')
    const field = await within(risk).findByLabelText('Max daily loss')

    expect(within(risk).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    fireEvent.change(field, { target: { value: '15' } })
    expect(within(risk).getAllByRole('button', { name: 'Save' })).toHaveLength(1)
  })

  /** Lowering a ceiling reduces what is at risk. Asking twice for the safe
   * direction is how you train someone to dismiss the dialog that matters. */
  it('commits a lowered limit without a confirm, as a string on the wire', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max daily loss'), {
      target: { value: '15' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() => expect(writesTo('/settings/limits')).toHaveLength(1))
    // A ceiling is stored exactly: the server refuses a JSON float, so the
    // value leaves as a quoted string whether or not it has a fraction.
    expect(writesTo('/settings/limits')[0]).toEqual({
      limits: [{ key: 'max_daily_loss_pct', value: '15' }],
    })
  })

  /** The bug this migration exists to fix: the confirm used to quote a
   * consequence computed from $100,000 of fixture money. Rule 4's point is
   * that the number a human approves is the real one. */
  it('quotes the raise consequence against the broker’s equity, not a fixture', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max risk per trade'), {
      target: { value: '10' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const dialog = screen.getByRole('alertdialog')
    // 7% and 10% of 99,901.08 — not of 100,000.
    expect(dialog.textContent).toContain('$6,993.08')
    expect(dialog.textContent).toContain('$9,990.11')
    expect(dialog.textContent).toContain('permit larger losses')
    expect(writesTo('/settings/limits')).toHaveLength(0)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Raise limit' }))
    await waitFor(() =>
      expect(writesTo('/settings/limits')[0]).toEqual({
        limits: [{ key: 'max_risk_per_trade_pct', value: '10' }],
      }),
    )
  })

  it('abandons a raise on cancel and writes nothing', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max risk per trade'), {
      target: { value: '10' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }),
    )

    expect(writesTo('/settings/limits')).toHaveLength(0)
  })

  /** The account is a separate request, and it can fail on its own — Cash
   * answers 409 while the live keys are absent. A `$0.00` consequence there
   * would be a fabricated figure on the one dialog whose job is a true one. */
  it('says why it cannot quote dollars rather than quoting zero', async () => {
    stubFetch({
      account: jsonResponse(409, {
        error: { code: 'account_unavailable', message: 'The cash account is not configured.' },
      }),
    })
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max risk per trade'), {
      target: { value: '10' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toMatch(/dollar figure cannot be quoted/)
    expect(dialog.textContent).not.toMatch(/\$0\.00/)
    // The percentage is still stated, because that is what is being approved.
    expect(dialog.textContent).toContain('7% to 10%')
  })

  /** `value: null` means no ceiling is configured. Not zero, not the shipped
   * default — and the page has to say so in words. */
  it('renders an unconfigured ceiling as an absence, and setting one is not a raise', async () => {
    served.limits = served.limits.map((l) =>
      l.key === 'max_daily_loss_pct' ? { ...l, value: null } : l,
    )
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    const field = (await within(risk).findByLabelText('Max daily loss')) as HTMLInputElement
    expect(field.value).toBe('')
    expect(within(risk).getByText(/No ceiling is currently configured/)).toBeInTheDocument()

    fireEvent.change(field, { target: { value: '20' } })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    // Nothing to raise *from*: a confirm would have to quote a previous
    // figure nobody set.
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() => expect(writesTo('/settings/limits')).toHaveLength(1))
  })

  it('refuses an out-of-range value and offers no way to save it', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max risk per trade'), {
      target: { value: '500' },
    })

    expect(within(risk).getByRole('alert').textContent).toMatch(/Maximum is 25%/)
    expect(within(risk).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  it('refuses a fractional position count', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max concurrent positions'), {
      target: { value: '8.5' },
    })

    expect(within(risk).getByRole('alert').textContent).toMatch(/whole number/)
  })

  /** The server validates every write and the page shows its refusal in the
   * server's own words — which name the rule. A control that silently
   * reverted would be rule 8's silent rejection wearing a checkbox. */
  it('renders the engine’s refusal of a write instead of reverting quietly', async () => {
    stubFetch({
      putLimits: refusal('max_risk_per_trade_pct must be between 1 and 25; got 26.'),
    })
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max daily loss'), {
      target: { value: '15' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const alert = await within(risk).findByRole('alert')
    expect(alert.textContent).toContain('must be between 1 and 25')
    expect(alert.textContent).toMatch(/Nothing was stored/)
  })
})

describe('notification routing', () => {
  it('renders every event the server routes, with both channels editable', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    expect(await within(routing).findAllByRole('checkbox')).toHaveLength(ROUTES.length * 2)
  })

  /** The whole row goes out, not the cell — the route is one record, and half
   * of it would let the other channel come back as whatever the request
   * defaulted to. */
  it('toggles a non-critical route immediately, sending both channels', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(await within(routing).findByLabelText('New recommendations ready — Bell'))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(writesTo('/settings/routes')[0]).toEqual({
        routes: [{ event: 'recommendations_ready', bell: false, discord: false }],
      }),
    )
  })

  it('marks the critical events so the confirm is not a surprise', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    expect(await within(routing).findAllByText('Critical')).toHaveLength(3)
  })

  /** Turning off one channel of a critical event is fine — the other still
   * delivers. Turning off the last one routes a rule-9 alert to nowhere. */
  it('lets a critical event lose one channel without ceremony', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(await within(routing).findByLabelText(/Engine error.*— Discord/))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(writesTo('/settings/routes')[0]).toEqual({
        routes: [{ event: 'engine_error', bell: true, discord: false }],
      }),
    )
  })

  it('confirms before silencing a critical event on every channel, and says what stops arriving', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(await within(routing).findByLabelText(/Engine error.*— Discord/))
    await waitFor(() => expect(writesTo('/settings/routes')).toHaveLength(1))
    fireEvent.click(await within(routing).findByLabelText(/Engine error.*— Bell/))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toContain('explicit human resume')
    // Not silenced until confirmed.
    expect(writesTo('/settings/routes')).toHaveLength(1)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Silence it' }))
    await waitFor(() =>
      expect(writesTo('/settings/routes')[1]).toEqual({
        routes: [{ event: 'engine_error', bell: false, discord: false }],
      }),
    )
  })

  it('leaves the route alone when the silence confirm is cancelled', async () => {
    stubFetch()
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(await within(routing).findByLabelText(/Engine error.*— Discord/))
    await waitFor(() => expect(writesTo('/settings/routes')).toHaveLength(1))
    fireEvent.click(await within(routing).findByLabelText(/Engine error.*— Bell/))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }),
    )

    expect(writesTo('/settings/routes')).toHaveLength(1)
  })

  /** A route with no webhook behind it delivers nothing, and that belongs on
   * the page that configures the route rather than only where the key is
   * listed. */
  it('stays quiet while the webhook is configured', async () => {
    stubFetch()
    render(<App />)
    await within(section('Notification routing')).findAllByRole('checkbox')

    expect(screen.queryByText(/DISCORD_WEBHOOK_URL is not set/)).not.toBeInTheDocument()
  })

  it('warns when Discord routes exist with no webhook behind them', async () => {
    stubFetch({
      keys: jsonResponse(
        200,
        KEYS.map((k) => (k.envVar === 'DISCORD_WEBHOOK_URL' ? { ...k, present: false } : k)),
      ),
    })
    render(<App />)

    const warning = await screen.findByText(/DISCORD_WEBHOOK_URL is not set/)
    expect(warning.textContent).toMatch(/none of them will be delivered/)
  })

  it('does not warn about a missing webhook when nothing routes to Discord', async () => {
    served.routes = served.routes.map((r) => ({ ...r, discord: false }))
    stubFetch({
      keys: jsonResponse(
        200,
        KEYS.map((k) => (k.envVar === 'DISCORD_WEBHOOK_URL' ? { ...k, present: false } : k)),
      ),
    })
    render(<App />)
    await within(section('Notification routing')).findAllByRole('checkbox')

    expect(screen.queryByText(/DISCORD_WEBHOOK_URL is not set/)).not.toBeInTheDocument()
  })
})

describe('data sources', () => {
  it('shows the server’s provider health, keeping degraded distinct from disconnected', async () => {
    stubFetch()
    render(<App />)
    const sources = section('Data sources')

    expect(await within(sources).findByText('Degraded')).toBeInTheDocument()
    expect(within(sources).getByText('Connected')).toBeInTheDocument()
    expect(within(sources).getByText('Disconnected')).toBeInTheDocument()
    expect(within(sources).getByText(/Engine halted/)).toBeInTheDocument()
  })

  /** Three figures, each named for what it governs. The websocket cap is two
   * budgets and not one — an equity symbol cap and an option quote cap — and
   * saying "30 symbols" without which stream is the error 56e7041 fixed on
   * three screens. */
  it('names the plan and the three caps it determines, per stream', async () => {
    stubFetch()
    render(<App />)
    const sources = section('Data sources')

    expect(within(sources).getByText('Basic (free)')).toBeInTheDocument()
    expect(within(sources).getByText(/30 streamed equity symbols/)).toBeInTheDocument()
    expect(within(sources).getByText(/200 streamed option quotes/)).toBeInTheDocument()
    expect(within(sources).getByText(/200 requests per minute/)).toBeInTheDocument()
  })

  /** Unlimited is an equities-only sentinel. This branch used to read
   * "unlimited streamed symbols", which was false for options: the paid plan
   * raises the option budget from 200 to 1,000 rather than removing it. */
  it('keeps a real option ceiling on Algo Trader Plus, where equities go unlimited', async () => {
    stubFetch()
    useUIStore.setState({ dataPlan: 'algo_trader_plus' })
    render(<App />)
    const sources = section('Data sources')

    expect(within(sources).getByText('Algo Trader Plus')).toBeInTheDocument()
    expect(within(sources).getByText(/unlimited streamed equity symbols/)).toBeInTheDocument()
    expect(within(sources).getByText(/1,000 streamed option quotes/)).toBeInTheDocument()
    expect(within(sources).queryByText(/unlimited streamed option/)).not.toBeInTheDocument()
    expect(within(sources).queryByText(/unlimited streamed symbols/)).not.toBeInTheDocument()
  })

  /** Requesting OPRA on Basic returns an auth error rather than empty data, so
   * the option is shown and disabled rather than hidden — knowing what an
   * upgrade buys is the point. The mark is a hint; the server is what refuses. */
  it('disables the feeds the plan cannot serve instead of hiding them', async () => {
    stubFetch()
    render(<App />)
    const sources = section('Data sources')

    const options = await within(sources).findByLabelText(/Options quotes/)
    expect(within(options).getByRole('option', { name: /OPRA/ })).toBeDisabled()
    expect(within(options).getByRole('option', { name: /Indicative/ })).not.toBeDisabled()

    const realtime = within(sources).getByLabelText(/Equity quotes \(real-time\)/)
    expect(within(realtime).getByRole('option', { name: /SIP/ })).toBeDisabled()
  })

  /** Historical SIP is free on Basic. Getting this wrong the other way would
   * hide the correct default behind a paywall that does not exist. */
  it('leaves historical SIP selectable on Basic', async () => {
    stubFetch()
    render(<App />)
    const historical = await within(section('Data sources')).findByLabelText(
      /Equity bars \(historical\)/,
    )
    expect(within(historical).getByRole('option', { name: /SIP/ })).not.toBeDisabled()
  })

  it('changes a feed and warns when historical bars drop to IEX', async () => {
    stubFetch()
    render(<App />)
    const sources = section('Data sources')

    fireEvent.change(await within(sources).findByLabelText(/Equity bars \(historical\)/), {
      target: { value: 'iex' },
    })

    await waitFor(() =>
      expect(writesTo('/settings/feeds')[0]).toEqual({
        feeds: [{ key: 'stockHistorical', value: 'iex' }],
      }),
    )
    // Rendered off the server's answer, not off the click.
    expect(await within(sources).findByText(/fortieth of real volume/)).toBeInTheDocument()
  })

  it('renders the engine’s refusal of an unentitled feed', async () => {
    stubFetch({
      putFeeds: refusal(
        "'opra' needs a plan upgrade. This account is on Basic (free), and Alpaca answers an " +
          'unentitled feed with an auth error.',
      ),
    })
    render(<App />)
    const sources = section('Data sources')

    fireEvent.change(await within(sources).findByLabelText(/Equity bars \(historical\)/), {
      target: { value: 'iex' },
    })

    const alert = await within(sources).findByRole('alert')
    expect(alert.textContent).toMatch(/needs a plan upgrade/)
  })
})

describe('appearance', () => {
  it('offers the theme toggle', () => {
    stubFetch()
    render(<App />)
    expect(within(section('Appearance')).getByRole('button')).toBeInTheDocument()
  })
})

describe('sentiment accuracy', () => {
  /** The readout has finished loading once its table is up. */
  async function sentimentTable(): Promise<HTMLElement> {
    // Not findByRole('table'): the loading skeleton is a table too.
    const header = await within(section('Sentiment accuracy')).findByRole('rowheader', {
      name: 'Corollary rules',
    })
    return header.closest('table') as HTMLElement
  }

  function row(table: HTMLElement, source: string): HTMLElement {
    return within(table).getByRole('rowheader', { name: source }).closest('tr') as HTMLElement
  }

  it('shows two rows, rules and Massive, with n beside each figure', async () => {
    stubFetch()
    render(<App />)
    const table = await sentimentTable()

    // Header row plus exactly two sources — the LLM and provider rows are gone.
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    const rules = row(table, 'Corollary rules')
    // 131 of 212 = 61.79…% → truncated to 61.7, never rounded up.
    expect(rules.textContent).toContain('61.7%')
    expect(rules.textContent).toContain('n 212')
    expect(rules.textContent).toContain('trailing 28 days')
    expect(row(table, 'Massive')).toBeInTheDocument()
  })

  it('renders the unaudited state as never audited, with zeros as a sample fact', async () => {
    stubFetch({ sentiment: jsonResponse(200, SENTIMENT_AUDIT_STATES.unaudited) })
    render(<App />)
    const table = await sentimentTable()
    const panel = section('Sentiment accuracy')

    expect(within(row(table, 'Corollary rules')).getByText('Never audited.')).toBeInTheDocument()
    expect(within(row(table, 'Massive')).getByText('Never audited.')).toBeInTheDocument()
    expect(within(table).queryByText(/Graded, but not yet/)).not.toBeInTheDocument()
    expect(within(table).getAllByText('insufficient sample — 0 of 30')).toHaveLength(4)
    expect(within(panel).getByText(/No audit has completed yet/)).toBeInTheDocument()
    expect(within(panel).getByText('No run has ever started.')).toBeInTheDocument()
    expect(within(panel).queryByRole('status')).not.toBeInTheDocument()
  })

  it('renders an active source with since-when in New York time', async () => {
    stubFetch()
    render(<App />)
    const rules = row(await sentimentTable(), 'Corollary rules')

    expect(within(rules).getByText('Active')).toBeInTheDocument()
    // 2026-10-03T14:05:12Z is 10:05 AM in New York (EDT).
    expect(rules.textContent).toMatch(/Active since Oct 3,? 10:05\sAM/)
  })

  it('renders a demoted source with its re-promotion progress, and the banner from the server', async () => {
    stubFetch()
    render(<App />)
    const massive = row(await sentimentTable(), 'Massive')
    const panel = section('Sentiment accuracy')

    expect(within(massive).getByText('Demoted')).toBeInTheDocument()
    expect(
      within(massive).getByText('since demotion: 41 of 100 in the 1h window'),
    ).toBeInTheDocument()
    expect(
      within(massive).getByText('since demotion: 37 of 100 in the 1d window'),
    ).toBeInTheDocument()
    // Judged on since_demotion: 41 graded, below the 100 floor.
    expect(within(massive).getByText('insufficient sample — 41 of 100')).toBeInTheDocument()

    const banner = within(panel).getByRole('status')
    expect(banner.textContent).toContain('Massive')
    expect(banner.textContent).toContain('display-only')
    expect(banner.textContent).not.toContain('Corollary rules')
  })

  /** Below the floor a percentage is not yet a measurement. The state is a
   * sample-size fact — `on-surface-variant`, never `caution`. */
  it('renders an insufficient sample as a fact, not a caution', async () => {
    stubFetch({ sentiment: jsonResponse(200, SENTIMENT_AUDIT_STATES.insufficientSample) })
    render(<App />)
    const rules = row(await sentimentTable(), 'Corollary rules')

    const short = within(rules).getByText('insufficient sample — 18 of 30')
    expect(short.className).toContain('text-on-surface-variant')
    expect(short.className).not.toMatch(/caution|error|bearish|bullish/)
    expect(within(rules).getByText('insufficient sample — 12 of 30')).toBeInTheDocument()
    expect(rules.textContent).not.toContain('%')
  })

  /** A run graded it (`asOfRunId` is set) but it is short of the floor, so
   * no status change was written and `neverAudited` is still true. "Never
   * audited." beside figures a run produced would contradict itself. */
  it('says a graded-but-short source was graded, not that it was never audited', async () => {
    stubFetch({ sentiment: jsonResponse(200, SENTIMENT_AUDIT_STATES.insufficientSample) })
    render(<App />)
    const rules = row(await sentimentTable(), 'Corollary rules')

    expect(
      within(rules).getByText('Graded, but not yet on enough labels to be given a status.'),
    ).toBeInTheDocument()
    expect(within(rules).queryByText('Never audited.')).not.toBeInTheDocument()
  })

  /** The cell refuses a percentage below the floor; one click down, the
   * split must not state one either — watch alone is 9 of 13 = 69.2%. */
  it('expands a short cell into counts only, with no percentage', async () => {
    stubFetch({ sentiment: jsonResponse(200, SENTIMENT_AUDIT_STATES.insufficientSample) })
    render(<App />)
    const rules = row(await sentimentTable(), 'Corollary rules')

    const toggle = within(rules).getByRole('button', {
      name: /^insufficient sample — 18 of 30\s*— Corollary rules, 1 hour: show the watch and discovery split$/,
    })
    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(rules.textContent).toContain('n 13')
    expect(rules.textContent).toContain('n 5')
    expect(rules.textContent).not.toContain('69.2')
    expect(rules.textContent).not.toContain('%')
  })

  it('says a status row is missing, never that the source was never audited', async () => {
    stubFetch({ sentiment: jsonResponse(200, SENTIMENT_AUDIT_STATES.statusMissing) })
    render(<App />)
    const massive = row(await sentimentTable(), 'Massive')
    const panel = section('Sentiment accuracy')

    expect(within(massive).getByText('Status missing')).toBeInTheDocument()
    expect(within(massive).queryByText('Never audited.')).not.toBeInTheDocument()
    expect(within(panel).getByRole('alert').textContent).toMatch(/no stored status row/)
    // A missing row is not a demotion: no banner.
    expect(within(panel).queryByRole('status')).not.toBeInTheDocument()
  })

  it('states both thresholds and the dead band in words', async () => {
    stubFetch()
    render(<App />)
    await sentimentTable()
    const text = section('Sentiment accuracy').textContent ?? ''

    expect(text).toMatch(/falls below\s*52%/)
    expect(text).toMatch(/reach\s*66%\s*or better/)
    expect(text).toMatch(
      /An unaudited source becomes active when both windows are at\s*52%\s*or better, with at least\s*30\s*graded labels each over the trailing 28 days/,
    )
    expect(text).toMatch(/Between\s*52%\s*and\s*66%\s*an active or demoted source stays where it is/)
    expect(text).not.toMatch(/Between\s*52%\s*and\s*66%\s*a source stays/)
  })

  /** N3: every figure in the rule paragraph is set as data, including the
   * dead band's two. */
  it('sets the dead band figures in the data face', async () => {
    stubFetch()
    render(<App />)
    await sentimentTable()
    const paragraph = within(section('Sentiment accuracy')).getByText(/Between/, {
      selector: 'p',
    })
    const figures = Array.from(paragraph.querySelectorAll('span.text-data-sm')).map(
      (el) => el.textContent,
    )
    expect(figures).toEqual(['52%', '30', '52%', '30', '66%', '100', '52%', '66%'])
  })

  it('expands a cell into its watch and discovery split, labelled display only', async () => {
    stubFetch()
    render(<App />)
    const rules = row(await sentimentTable(), 'Corollary rules')

    expect(within(rules).queryByText(/display only/)).not.toBeInTheDocument()
    // The figure is the name (WCAG 2.5.3); the action follows it, hidden.
    const toggle = within(rules).getByRole('button', {
      name: /^61\.7%\s*n 212\s*— Corollary rules, 1 hour: show the watch and discovery split$/,
    })
    expect(toggle).not.toHaveAttribute('aria-label')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAccessibleName(
      /^61\.7%\s*n 212\s*— Corollary rules, 1 hour: hide the watch and discovery split$/,
    )
    expect(
      within(rules).getByText('display only — transitions act on the combined figure'),
    ).toBeInTheDocument()
    // Watch: 94 of 150 = 62.6%; discovery: 37 of 62 = 59.6%.
    expect(rules.textContent).toContain('62.6%')
    expect(rules.textContent).toContain('n 150')
    expect(rules.textContent).toContain('59.6%')
    expect(rules.textContent).toContain('n 62')
  })

  it('shows the last and next run in New York time', async () => {
    stubFetch()
    render(<App />)
    await sentimentTable()
    const text = section('Sentiment accuracy').textContent ?? ''

    // Started 14:00Z = 10:00 AM EDT; next run 2026-10-17T14:00Z = Sat 10:00 AM.
    expect(text).toMatch(/Last run\s*Oct 10,? 10:00\sAM \(scheduled, succeeded/)
    expect(text).toMatch(/Next run\s*Oct 17,? 10:00\sAM \(scheduled\)/)
  })

  /** Q6: the audit is the only writer of a source's status. */
  it('offers no re-promote or reinstate control anywhere on the page', async () => {
    stubFetch()
    render(<App />)
    await sentimentTable()

    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent ?? '').not.toMatch(/re-?promot|reinstat|restore|undemot/i)
      expect(button.getAttribute('aria-label') ?? '').not.toMatch(/re-?promot|reinstat/i)
    }
    const panel = section('Sentiment accuracy')
    expect(within(panel).getAllByRole('button').map((b) => b.textContent)).toContain(
      'Run audit now',
    )
  })

  /** Phase 3 step 6: the readout is the engine's now, and says so by carrying
   * no marker at all. */
  it('carries no fixture marker', async () => {
    stubFetch()
    render(<App />)
    await sentimentTable()

    expect(screen.queryByText('Sample data — Phase 1')).not.toBeInTheDocument()
    expect(screen.queryByText(/not the engine’s own configuration/)).not.toBeInTheDocument()
  })

  it('designs its loading and failed states', async () => {
    stubFetch({ sentiment: jsonResponse(500, { error: { code: 'internal', message: 'boom' } }) })
    render(<App />)

    expect(
      within(section('Sentiment accuracy')).getByText('Loading the sentiment audit'),
    ).toBeInTheDocument()
    // A 5xx is retried once (`shouldRetry`) before it renders.
    expect(
      await within(section('Sentiment accuracy')).findByRole('alert', {}, { timeout: 4000 }),
    ).toHaveTextContent('boom')
  })
})

describe('running the audit by hand', () => {
  async function run(overrides: Overrides = {}) {
    const fetchMock = stubFetch(overrides)
    render(<App />)
    const panel = section('Sentiment accuracy')
    fireEvent.click(await within(panel).findByRole('button', { name: 'Run audit now' }))
    return { panel, fetchMock }
  }

  it('says the run started on a 202, and re-reads the readout', async () => {
    const { panel, fetchMock } = await run()

    expect(await within(panel).findByText(/Audit run 8 started at/)).toBeInTheDocument()
    expect(writesTo('/settings/sentiment/audit')).toHaveLength(1)
    await waitFor(() => {
      const reads = fetchMock.mock.calls.filter(
        ([url, init]) =>
          String(url).includes('/settings/sentiment') && (init?.method ?? 'GET') === 'GET',
      )
      expect(reads.length).toBeGreaterThanOrEqual(2)
    })
  })

  /** "It takes a few minutes" is true only while the run it names is open.
   * Once the readout shows run 8 closed, `Last run` carries its outcome. */
  it('clears the started line once the readout shows that run closed', async () => {
    served.sentiment = SENTIMENT_AUDIT_STATES.running
    const { panel } = await run()

    // Run 8 is still open: the line stays.
    expect(await within(panel).findByText(/Audit run 8 started at/)).toBeInTheDocument()

    const open = SENTIMENT_AUDIT_STATES.running.lastRun!
    served.sentiment = {
      ...SENTIMENT_AUDIT_STATES.running,
      lastRun: { ...open, status: 'succeeded', finishedAt: '2026-10-14T15:26:00Z' },
    }
    await act(() => queryClient.invalidateQueries())

    await waitFor(() => {
      expect(within(panel).queryByText(/Audit run 8 started at/)).not.toBeInTheDocument()
    })
    expect(panel.textContent).toMatch(/started by hand, succeeded, finished/)
  })

  it('says when it is available again on a 429, from Retry-After', async () => {
    const { panel } = await run({
      postAudit: jsonResponse(
        429,
        {
          error: {
            code: 'sentiment_audit_rate_limited',
            message: 'a manual sentiment audit run already started in the last hour',
          },
        },
        { 'Retry-After': '1800' },
      ),
    })

    const line = await within(panel).findByText(/manual runs are limited to one an hour/)
    expect(line.textContent).toMatch(/next is available at/)
    expect(line.className).toContain('text-on-surface-variant')
  })

  it('says a run is in progress on a 409', async () => {
    const { panel } = await run({
      postAudit: jsonResponse(409, {
        error: {
          code: 'sentiment_audit_running',
          message: 'sentiment audit run 8 (scheduled) is still in progress',
        },
      }),
    })

    const line = await within(panel).findByText(/a run is already in progress/)
    expect(line.textContent).toContain('sentiment audit run 8 (scheduled) is still in progress')
    expect(line.className).not.toMatch(/error/)
  })

  /** A degraded data dependency is not a fault and not a loss: `caution`. */
  it('shows the server reason on a 503, in caution', async () => {
    const { panel } = await run({
      postAudit: jsonResponse(503, {
        error: {
          code: 'sentiment_audit_unavailable',
          message: 'the watch universe is degraded; nothing was started',
        },
      }),
    })

    const line = await within(panel).findByRole('alert')
    expect(line.textContent).toContain('the watch universe is degraded; nothing was started')
    expect(line.className).toContain('text-caution')
  })

  it('shows the server reason on a 500, in error', async () => {
    const { panel } = await run({
      postAudit: jsonResponse(500, {
        error: { code: 'internal_error', message: 'the universe read failed' },
      }),
    })

    const line = await within(panel).findByRole('alert')
    expect(line.textContent).toContain('the universe read failed')
    expect(line.className).toContain('text-error')
  })
})

describe('audit log', () => {
  it('explains an untouched log rather than rendering an empty table', async () => {
    stubFetch()
    render(<App />)
    const log = section('Configuration audit log')

    expect(await within(log).findByText(/still at the value it was seeded with/)).toBeInTheDocument()
    expect(within(log).queryByRole('table')).not.toBeInTheDocument()
  })

  /** The server keys a feed row by its **env var**, not by the camelCase key
   * the feed list uses. Matching only on the key printed
   * `ALPACA_STOCK_FEED_HISTORICAL` in a column that promises readable
   * settings. */
  it('renders server rows with readable setting names, including feeds keyed by env var', async () => {
    served.audit = [
      {
        id: 'a1',
        time: '2026-09-12T14:30:00Z',
        category: 'feed',
        field: 'ALPACA_STOCK_FEED_HISTORICAL',
        previousValue: 'sip',
        newValue: 'iex',
      },
      {
        id: 'a2',
        time: '2026-09-12T14:00:00Z',
        category: 'risk',
        field: 'max_risk_per_trade_pct',
        previousValue: '7',
        newValue: '10',
      },
      {
        id: 'a3',
        time: '2026-09-12T13:00:00Z',
        category: 'notification',
        field: 'recommendations_ready.bell',
        previousValue: 'on',
        newValue: 'off',
      },
    ]
    stubFetch()
    render(<App />)
    const log = section('Configuration audit log')

    expect(await within(log).findByText('Equity bars (historical)')).toBeInTheDocument()
    expect(within(log).getByText('Max risk per trade')).toBeInTheDocument()
    expect(within(log).getByText('New recommendations ready — Bell')).toBeInTheDocument()
    expect(within(log).queryByText('ALPACA_STOCK_FEED_HISTORICAL')).not.toBeInTheDocument()
    expect(within(log).queryByText('max_risk_per_trade_pct')).not.toBeInTheDocument()
  })

  /** Every config edit writes an audit row server-side, so a successful
   * mutation invalidates the log. A page that only invalidated its own list
   * would show a log that was one change out of date on the day it mattered. */
  it('picks up the row the engine recorded for a change made here', async () => {
    stubFetch()
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(await within(risk).findByLabelText('Max daily loss'), {
      target: { value: '15' },
    })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const log = section('Configuration audit log')
    const table = await within(log).findByRole('table')
    const rows = within(table).getAllByRole('row')
    expect(rows[1].textContent).toContain('Max daily loss')
    expect(rows[1].textContent).toContain('20')
    expect(rows[1].textContent).toContain('15')
  })

  it('says when it is showing a page of a longer log', async () => {
    served.audit = [
      {
        id: 'a1',
        time: '2026-09-12T14:30:00Z',
        category: 'risk',
        field: 'max_daily_loss_pct',
        previousValue: '20',
        newValue: '15',
      },
    ]
    stubFetch({ audit: jsonResponse(200, page(served.audit, 240)) })
    render(<App />)
    const log = section('Configuration audit log')

    expect(await within(log).findByText(/most recent 1 of 240/)).toBeInTheDocument()
  })
})
