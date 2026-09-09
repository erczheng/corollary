import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS } from '../lib/mockData'

const initialState = useUIStore.getState()
const PAPER = ACCOUNT_SNAPSHOTS.paper

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState, lastTickAt: new Date().toISOString() }, true)
})

/** The per-trade risk ceiling is quoted in two different tickets, and it is
 * now editable in Settings. These tests exist because of how the two used to
 * read it.
 *
 * `OrderTicket` did `RISK_LIMITS.find(…)?.value ?? 0` *inside* the component,
 * so it was merely stale. `ChainOrderTicket` did the same lookup with `?? 7`
 * at **module scope** — evaluated once when the module was first imported,
 * which means an edited limit could never reach it again for the life of the
 * tab, through any number of remounts. Both tests below fail against that
 * version even if you hand it a store, which is exactly why they are worth
 * having rather than assuming the wiring is right.
 *
 * None of this enforces anything. CLAUDE.md rule 4: the engine enforces and
 * never trusts a limit that arrived from the client. These are advisory
 * readouts, and being advisory is not a licence to be wrong. */
describe('the per-trade risk ceiling follows the store', () => {
  function openAddTicket(): void {
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Activity' }))

    const target = PAPER.positions[0]
    const row = screen
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(target.contract))!

    fireEvent.click(within(row).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to position' }))
  }

  it('quotes the fixture default in the position ticket before anything is edited', () => {
    openAddTicket()
    expect(screen.getByText(/against a 7% per-trade ceiling/)).toBeInTheDocument()
  })

  it('quotes the edited ceiling in the position ticket', () => {
    useUIStore.getState().setRiskLimit('max_risk_per_trade_pct', 12)
    openAddTicket()

    expect(screen.getByText(/against a 12% per-trade ceiling/)).toBeInTheDocument()
    expect(screen.queryByText(/against a 7% per-trade ceiling/)).not.toBeInTheDocument()
  })

  /** With no ceiling configured the ticket says so rather than printing a
   * number. The old `?? 0` reported a 0% ceiling, which describes an account
   * that may not trade at all — a claim nobody made. */
  it('says no ceiling is configured rather than inventing one', () => {
    useUIStore.setState({ riskLimits: [] })
    openAddTicket()

    expect(screen.getByText(/No per-trade ceiling is configured/)).toBeInTheDocument()
  })

  function openChainTicket(): void {
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Markets' }))

    const chain = within(screen.getByRole('region', { name: 'Options chains' })).getByRole('table')
    fireEvent.click(within(chain).getAllByRole('button', { name: /^Trade/ })[0])
  }

  /** The module-scope case. A 1% ceiling puts a single contract over it, so
   * the advisory warning has to appear *and* name the edited figure. */
  it('quotes the edited ceiling in the chain ticket, which used to capture it at import', () => {
    useUIStore.getState().setRiskLimit('max_risk_per_trade_pct', 1)
    openChainTicket()

    const warning = screen.getByText(/per-trade ceiling\. The engine enforces this limit/)
    expect(warning.textContent).toContain('1% per-trade ceiling')
    expect(warning.textContent).not.toContain('7% per-trade ceiling')
  })

  it('raises no over-limit warning in the chain ticket when no ceiling is configured', () => {
    useUIStore.setState({ riskLimits: [] })
    openChainTicket()

    expect(
      screen.queryByText(/per-trade ceiling\. The engine enforces this limit/),
    ).not.toBeInTheDocument()
  })
})
