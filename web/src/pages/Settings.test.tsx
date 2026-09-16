import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { SENTIMENT_ACCURACY } from '../lib/mockData'
import type {
  AccountResponse,
  ApiKeyPresence,
  AuditLogEntry,
  DataFeed,
  DataSourceStatus,
  NotificationRoute,
  Page,
  RiskLimit,
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
  { event: 'price_alert', bell: true, discord: true },
  { event: 'recommendations_ready', bell: true, discord: false },
  { event: 'strategy_promotion', bell: true, discord: false },
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

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
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
  it('lists every source against the floor', () => {
    stubFetch()
    render(<App />)
    const table = within(section('Sentiment accuracy')).getByRole('table')

    for (const row of SENTIMENT_ACCURACY) {
      expect(within(table).getByText(row.source)).toBeInTheDocument()
    }
  })

  /** PRD.md §9: below 52% the source is demoted from a scanner input to
   * display-only. Without a sub-52% fixture this state would be unreachable
   * and therefore invisible. */
  it('names the demoted source and says what demotion means', () => {
    stubFetch()
    render(<App />)
    const banner = within(section('Sentiment accuracy')).getByRole('status')

    expect(banner.textContent).toContain('StockTwits')
    expect(banner.textContent).toContain('display-only')
    expect(banner.textContent).toMatch(/scanner no longer reads it/)
  })

  /** The one panel here that is still a fixture, and the only one on the page.
   * Unmarked, a table of invented percentages reads as measured now that
   * every panel beside it is the engine's own configuration. */
  it('marks itself as sample data, in the same words as the scripted chat', () => {
    stubFetch()
    render(<App />)
    const sentiment = section('Sentiment accuracy')

    expect(within(sentiment).getByText('Sample data — Phase 1')).toBeInTheDocument()
    expect(
      within(sentiment).getByText(/not the engine’s own configuration/),
    ).toBeInTheDocument()
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

/** Phase 2 decision 8, and the half of it that is specific to this page:
 * Settings mixes real and mock inside one page, so the marker sits on the
 * affected panel. Over the title it would label the engine's own risk
 * ceilings as invented, which is worse than no marker at all. */
describe('the fixture marker', () => {
  it('is scoped to the sentiment panel and marks nothing that the server serves', async () => {
    stubFetch()
    render(<App />)

    // Wait for the served panels to finish loading first: a skeleton carries
    // no marker either, so an unawaited assertion would pass for the wrong
    // reason.
    await within(section('Risk limits')).findByLabelText('Max daily loss')

    const marker = screen.getByText('Sample data — Phase 1')
    expect(marker.closest('section')).toBe(section('Sentiment accuracy'))

    for (const name of [
      'API keys',
      'Risk limits',
      'Notification routing',
      'Data sources',
      'Configuration audit log',
    ]) {
      expect(within(section(name)).queryByText('Sample data — Phase 1')).not.toBeInTheDocument()
    }
  })

  it('appears exactly once on a page that is mostly server-backed', async () => {
    stubFetch()
    render(<App />)
    await within(section('Risk limits')).findByLabelText('Max daily loss')

    expect(screen.getAllByText('Sample data — Phase 1')).toHaveLength(1)
  })

  /** News and Research carry a screen-reader copy of the marker's detail,
   * because a tooltip on a non-focusable span is mouse-only and neither page
   * restates it. This panel does restate it, visibly and to everyone, so the
   * copy is suppressed here — a description repeated verbatim a second later
   * is how a screen-reader user learns to stop listening to them. */
  it('does not say in a screen reader what the panel already says in prose', async () => {
    stubFetch()
    render(<App />)
    await within(section('Risk limits')).findByLabelText('Max daily loss')
    const sentiment = section('Sentiment accuracy')

    expect(within(sentiment).getByText('Sample data — Phase 1').getAttribute('title')).toMatch(
      /Nothing has scored a sentiment label/,
    )
    expect(screen.queryByText(/Nothing has scored a sentiment label/)).not.toBeInTheDocument()
    expect(within(sentiment).getByText(/not the engine’s own configuration/)).toBeInTheDocument()
  })
})
