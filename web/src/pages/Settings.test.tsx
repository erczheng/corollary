import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { NOTIFICATION_ROUTES, SENTIMENT_ACCURACY } from '../lib/mockData'

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/settings')
  useUIStore.setState({ ...initialState }, true)
})

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

describe('API keys', () => {
  it('lists each variable with its presence and nothing else', () => {
    render(<App />)
    const table = within(section('API keys')).getByRole('table')

    for (const key of initialState.apiKeys) {
      expect(within(table).getByText(key.envVar)).toBeInTheDocument()
    }
    expect(within(table).getAllByText('Set').length).toBeGreaterThan(0)
    expect(within(table).getAllByText('Not set').length).toBeGreaterThan(0)
  })

  /** CLAUDE.md rule 6: the UI never renders a key. Not a masked one, not a
   * last-four — there is no element on this page that could contain key
   * material, because the fixture carries no value to render. */
  it('renders no field that could hold key material', () => {
    render(<App />)
    const keys = section('API keys')

    expect(within(keys).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(keys).queryByText(/•/)).not.toBeInTheDocument()
    expect(keys.textContent ?? '').not.toMatch(/PK[A-Z0-9]{8}/)
  })

  it('says the values are never sent to the browser', () => {
    render(<App />)
    expect(within(section('API keys')).getByText(/never displayed here/)).toBeInTheDocument()
  })
})

describe('risk limits', () => {
  /** CLAUDE.md: a percentage ceiling is uninterpretable without these three
   * definitions, so the page states them rather than leaving each reader to
   * assume one. */
  it('states what risk means for each structure type', () => {
    render(<App />)
    const risk = section('Risk limits')

    expect(within(risk).getByText(/maximum loss at expiry/)).toBeInTheDocument()
    expect(within(risk).getByText(/premium paid/)).toBeInTheDocument()
    expect(within(risk).getByText(/±2σ/)).toBeInTheDocument()
  })

  it('says the engine enforces and this page only stores', () => {
    render(<App />)
    expect(within(section('Risk limits')).getByText(/never trusts a value sent from this page/)).toBeInTheDocument()
  })

  it('shows no Save button until the value actually changes', () => {
    render(<App />)
    const risk = section('Risk limits')
    expect(within(risk).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()

    fireEvent.change(within(risk).getByLabelText('Max daily loss'), { target: { value: '15' } })
    expect(within(risk).getAllByRole('button', { name: 'Save' })).toHaveLength(1)
  })

  /** Lowering a ceiling reduces what is at risk. Asking twice for the safe
   * direction is how you train someone to dismiss the dialog that matters. */
  it('commits a lowered limit without a confirm', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max daily loss'), { target: { value: '15' } })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(useUIStore.getState().riskLimits.find((l) => l.key === 'max_daily_loss_pct')!.value).toBe(15)
  })

  /** A raise loosens the ceiling, and the confirm has to state the
   * consequence in money — "7% to 10%" is not something anyone can feel. */
  it('confirms a raise and quotes the consequence in dollars', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max risk per trade'), { target: { value: '10' } })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toMatch(/\$[\d,]+\.\d{2}/)
    expect(dialog.textContent).toContain('permit larger losses')
    // Not yet committed.
    expect(useUIStore.getState().riskLimits.find((l) => l.key === 'max_risk_per_trade_pct')!.value).toBe(7)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Raise limit' }))
    expect(useUIStore.getState().riskLimits.find((l) => l.key === 'max_risk_per_trade_pct')!.value).toBe(10)
  })

  it('abandons a raise on cancel', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max risk per trade'), { target: { value: '10' } })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    expect(useUIStore.getState().riskLimits.find((l) => l.key === 'max_risk_per_trade_pct')!.value).toBe(7)
  })

  it('refuses an out-of-range value and offers no way to save it', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max risk per trade'), { target: { value: '500' } })

    expect(within(risk).getByRole('alert').textContent).toMatch(/Maximum is 25%/)
    expect(within(risk).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
  })

  it('refuses a fractional position count', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max concurrent positions'), {
      target: { value: '8.5' },
    })

    expect(within(risk).getByRole('alert').textContent).toMatch(/whole number/)
  })
})

describe('notification routing', () => {
  it('renders every event with both channels editable', () => {
    render(<App />)
    const table = within(section('Notification routing')).getByRole('table')

    expect(within(table).getAllByRole('checkbox')).toHaveLength(NOTIFICATION_ROUTES.length * 2)
  })

  it('toggles a non-critical route immediately', () => {
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(within(routing).getByLabelText('New recommendations ready — Bell'))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(
      useUIStore.getState().notificationRoutes.find((r) => r.event === 'recommendations_ready')!.bell,
    ).toBe(false)
  })

  it('marks the critical events so the confirm is not a surprise', () => {
    render(<App />)
    expect(within(section('Notification routing')).getAllByText('Critical')).toHaveLength(3)
  })

  /** Turning off one channel of a critical event is fine — the other still
   * delivers. Turning off the last one routes a rule-9 alert to nowhere. */
  it('lets a critical event lose one channel without ceremony', () => {
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(within(routing).getByLabelText(/Engine error.*— Discord/))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(useUIStore.getState().notificationRoutes.find((r) => r.event === 'engine_error')!.discord).toBe(false)
  })

  it('confirms before silencing a critical event on every channel, and says what stops arriving', () => {
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(within(routing).getByLabelText(/Engine error.*— Discord/))
    fireEvent.click(within(routing).getByLabelText(/Engine error.*— Bell/))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toContain('explicit human resume')
    // Not silenced until confirmed.
    expect(useUIStore.getState().notificationRoutes.find((r) => r.event === 'engine_error')!.bell).toBe(true)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Silence it' }))
    expect(useUIStore.getState().notificationRoutes.find((r) => r.event === 'engine_error')!.bell).toBe(false)
  })

  it('leaves the route alone when the silence confirm is cancelled', () => {
    render(<App />)
    const routing = section('Notification routing')

    fireEvent.click(within(routing).getByLabelText(/Engine error.*— Discord/))
    fireEvent.click(within(routing).getByLabelText(/Engine error.*— Bell/))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    expect(useUIStore.getState().notificationRoutes.find((r) => r.event === 'engine_error')!.bell).toBe(true)
  })

  /** A route with no webhook behind it delivers nothing, and that belongs on
   * the page that configures the route rather than only where the key is
   * listed. */
  it('stays quiet while the webhook is configured', () => {
    render(<App />)
    expect(screen.queryByText(/DISCORD_WEBHOOK_URL is not set/)).not.toBeInTheDocument()
  })

  it('warns when Discord routes exist with no webhook behind them', () => {
    useUIStore.setState({
      apiKeys: initialState.apiKeys.map((k) =>
        k.envVar === 'DISCORD_WEBHOOK_URL' ? { ...k, present: false } : k,
      ),
    })
    render(<App />)

    const warning = screen.getByText(/DISCORD_WEBHOOK_URL is not set/)
    expect(warning.textContent).toMatch(/none of them will be delivered/)
  })

  it('does not warn about a missing webhook when nothing routes to Discord', () => {
    useUIStore.setState({
      apiKeys: initialState.apiKeys.map((k) =>
        k.envVar === 'DISCORD_WEBHOOK_URL' ? { ...k, present: false } : k,
      ),
      notificationRoutes: initialState.notificationRoutes.map((r) => ({ ...r, discord: false })),
    })
    render(<App />)

    expect(screen.queryByText(/DISCORD_WEBHOOK_URL is not set/)).not.toBeInTheDocument()
  })
})

describe('data sources', () => {
  it('shows provider status, keeping degraded distinct from disconnected', () => {
    render(<App />)
    const sources = section('Data sources')

    expect(within(sources).getAllByText('Connected').length).toBeGreaterThan(0)
    expect(within(sources).getByText('Degraded')).toBeInTheDocument()
  })

  it('names the plan and the two caps it determines', () => {
    render(<App />)
    const sources = section('Data sources')

    expect(within(sources).getByText('Basic (free)')).toBeInTheDocument()
    expect(within(sources).getByText(/30 streamed symbols/)).toBeInTheDocument()
    expect(within(sources).getByText(/200 requests per minute/)).toBeInTheDocument()
  })

  /** Requesting OPRA on Basic returns an auth error rather than empty data, so
   * the option is shown and disabled rather than hidden — knowing what an
   * upgrade buys is the point. */
  it('disables the feeds the plan cannot serve instead of hiding them', () => {
    render(<App />)
    const sources = section('Data sources')

    const options = within(sources).getByLabelText(/Options quotes/)
    expect(within(options).getByRole('option', { name: /OPRA/ })).toBeDisabled()
    expect(within(options).getByRole('option', { name: /Indicative/ })).not.toBeDisabled()

    const realtime = within(sources).getByLabelText(/Equity quotes \(real-time\)/)
    expect(within(realtime).getByRole('option', { name: /SIP/ })).toBeDisabled()
  })

  /** Historical SIP is free on Basic. Getting this wrong the other way would
   * hide the correct default behind a paywall that does not exist. */
  it('leaves historical SIP selectable on Basic', () => {
    render(<App />)
    const historical = within(section('Data sources')).getByLabelText(/Equity bars \(historical\)/)
    expect(within(historical).getByRole('option', { name: /SIP/ })).not.toBeDisabled()
  })

  it('changes a feed and warns when historical bars drop to IEX', () => {
    render(<App />)
    const sources = section('Data sources')

    fireEvent.change(within(sources).getByLabelText(/Equity bars \(historical\)/), {
      target: { value: 'iex' },
    })

    expect(useUIStore.getState().dataFeeds.find((f) => f.key === 'stockHistorical')!.value).toBe('iex')
    expect(within(sources).getByText(/fortieth of real volume/)).toBeInTheDocument()
  })
})

describe('appearance', () => {
  it('offers the theme toggle', () => {
    render(<App />)
    expect(within(section('Appearance')).getByRole('button')).toBeInTheDocument()
  })
})

describe('sentiment accuracy', () => {
  it('lists every source against the floor', () => {
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
    render(<App />)
    const banner = within(section('Sentiment accuracy')).getByRole('status')

    expect(banner.textContent).toContain('StockTwits')
    expect(banner.textContent).toContain('display-only')
    expect(banner.textContent).toMatch(/scanner no longer reads it/)
  })
})

describe('audit log', () => {
  it('renders the seeded entries with readable setting names, not raw keys', () => {
    render(<App />)
    const log = section('Configuration audit log')

    expect(within(log).getByText('Max risk per trade')).toBeInTheDocument()
    expect(within(log).getByText('Max concurrent positions')).toBeInTheDocument()
    // The feed row proves one log carries all three categories.
    expect(within(log).getByText('Equity bars (historical)')).toBeInTheDocument()
    expect(within(log).queryByText('max_risk_per_trade_pct')).not.toBeInTheDocument()
  })

  it('explains an untouched log rather than rendering an empty table', () => {
    useUIStore.setState({ auditLog: [] })
    render(<App />)
    const log = section('Configuration audit log')

    expect(within(log).queryByRole('table')).not.toBeInTheDocument()
    expect(within(log).getByText(/still at its default/)).toBeInTheDocument()
  })

  it('appends a row when a limit is changed, newest first', () => {
    render(<App />)
    const risk = section('Risk limits')

    fireEvent.change(within(risk).getByLabelText('Max daily loss'), { target: { value: '15' } })
    fireEvent.click(within(risk).getByRole('button', { name: 'Save' }))

    const rows = within(within(section('Configuration audit log')).getByRole('table')).getAllByRole('row')
    // Header plus the seeded entries plus the new one.
    expect(rows).toHaveLength(1 + initialState.auditLog.length + 1)
    expect(rows[1].textContent).toContain('Max daily loss')
    expect(rows[1].textContent).toContain('20')
    expect(rows[1].textContent).toContain('15')
  })

  it('appends a row when a feed is changed', () => {
    render(<App />)

    fireEvent.change(within(section('Data sources')).getByLabelText(/Equity bars \(historical\)/), {
      target: { value: 'iex' },
    })

    const rows = within(within(section('Configuration audit log')).getByRole('table')).getAllByRole('row')
    expect(rows[1].textContent).toContain('Equity bars (historical)')
    expect(rows[1].textContent).toContain('sip')
    expect(rows[1].textContent).toContain('iex')
  })

  it('appends a row when a route is changed', () => {
    render(<App />)

    fireEvent.click(
      within(section('Notification routing')).getByLabelText('New recommendations ready — Bell'),
    )

    const rows = within(within(section('Configuration audit log')).getByRole('table')).getAllByRole('row')
    expect(rows[1].textContent).toContain('New recommendations ready — Bell')
    expect(rows[1].textContent).toContain('on')
    expect(rows[1].textContent).toContain('off')
  })
})
