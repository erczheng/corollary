import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS, STRATEGIES } from '../lib/mockData'
import { formatUsd } from '../lib/format'

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location, and these tests navigate. Without
  // this, a test that ran after a navigation starts on the wrong page.
  // Same guard Activity.test.tsx carries, for the same reason.
  window.history.pushState({}, '', '/')
  // `lastTickAt` seeded for the one test that navigates to Activity, which
  // renders skeletons until a price has arrived. Same reason as
  // Activity.test.tsx.
  useUIStore.setState({ ...initialState, lastTickAt: new Date().toISOString() }, true)
})

function lastValue(history: { value: number }[]): number {
  return history[history.length - 1].value
}

describe('Dashboard header stats follow the account toggle', () => {
  it('shows the paper balance and volume on a cold start', () => {
    render(<App />)

    const paper = ACCOUNT_SNAPSHOTS.paper
    expect(screen.getByText(formatUsd(lastValue(paper.portfolioHistory)))).toBeInTheDocument()
    expect(screen.getByText(formatUsd(paper.volume24h))).toBeInTheDocument()
  })

  it('moves both to the cash account once the switch is confirmed', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )

    const cash = ACCOUNT_SNAPSHOTS.cash
    const paper = ACCOUNT_SNAPSHOTS.paper

    expect(screen.getByText(formatUsd(lastValue(cash.portfolioHistory)))).toBeInTheDocument()
    expect(screen.getByText(formatUsd(cash.volume24h))).toBeInTheDocument()
    // The point of the fix: paper's money is no longer on screen while a
    // real-money account is selected.
    expect(
      screen.queryByText(formatUsd(lastValue(paper.portfolioHistory))),
    ).not.toBeInTheDocument()
    expect(screen.queryByText(formatUsd(paper.volume24h))).not.toBeInTheDocument()
  })
})

describe('Live win rate stat', () => {
  it('shows the live rate and its gap to backtest for a strategy that has traded', () => {
    render(<App />)

    const strat = STRATEGIES.find((s) => s.id === 'strat-1')!
    const card = screen.getByRole('group', { name: 'Live win rate' })

    expect(within(card).getByText(`${strat.live!.winRate}%`)).toBeInTheDocument()
    // 71 live against 68 backtested is +3 points, not +3 percent.
    expect(within(card).getByText('+3.0 pts')).toBeInTheDocument()
  })

  it('shows an em dash, not the backtested number, when the strategy has never traded live', () => {
    render(<App />)

    const backtestOnly = STRATEGIES.find((s) => s.live === null)!
    fireEvent.change(screen.getByRole('combobox', { name: 'Active strategy' }), {
      target: { value: backtestOnly.id },
    })

    const card = screen.getByRole('group', { name: 'Live win rate' })
    expect(within(card).getByText('—')).toBeInTheDocument()
    expect(
      within(card).queryByText(`${backtestOnly.backtest.winRate}%`),
    ).not.toBeInTheDocument()
    expect(within(card).getByText(/Not traded live yet/)).toBeInTheDocument()
  })
})

function section(heading: string): HTMLElement {
  return screen.getByRole('heading', { name: heading }).closest('section')!
}

/** "Trading On" is reserved for Auto — the only mode in which the engine
 * acts without a click. Manual reports itself as Manual and has no halted
 * state, but keeps Flatten, because open positions are open however they
 * were opened. */
describe('Trading-state pill', () => {
  it('reads Manual on a cold start, with no halt control offered', () => {
    render(<App />)

    expect(screen.getByText(/^Manual \(Paper\)$/)).toBeInTheDocument()
    expect(screen.queryByText(/Trading On/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Halt' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resume trading' })).not.toBeInTheDocument()
  })

  it('reads Trading On only once the engine is allowed to act unattended', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))

    expect(screen.getByText(/^Trading On \(Paper\)$/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Halt' })).toBeInTheDocument()
  })

  it('reads Halted in Auto once halted, and Manual again on the way back', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    fireEvent.click(screen.getByRole('button', { name: 'Halt' }))
    expect(screen.getByText(/^Halted \(Paper\)$/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Manual' }))
    expect(screen.getByText(/^Manual \(Paper\)$/)).toBeInTheDocument()
    expect(screen.queryByText(/Halted/)).not.toBeInTheDocument()
  })

  it('keeps Flatten usable in Manual', () => {
    render(<App />)

    expect(screen.getByRole('button', { name: 'Flatten' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Flatten' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Flatten' }),
    )

    expect(useUIStore.getState().openPositions.paper).toHaveLength(0)
    // Flatten still halts underneath — it just has nothing to say in
    // Manual. Switching to Auto has to surface it rather than come up
    // trading (CLAUDE.md rule 9: never auto-resume).
    expect(screen.getByText(/^Manual \(Paper\)$/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    expect(screen.getByText(/^Halted \(Paper\)$/)).toBeInTheDocument()
  })

  it('leaves the Trade buttons live in Manual, since halt does not apply there', () => {
    render(<App />)

    // act() so the store update actually reaches the component — otherwise
    // this asserts on a render that never saw the halt, and passes for the
    // wrong reason.
    act(() => useUIStore.getState().halt())
    expect(useUIStore.getState().isHalted).toBe(true)

    const trade = screen.getAllByRole('button', { name: 'Trade' })
    expect(trade.length).toBeGreaterThan(0)
    for (const b of trade) expect(b).toBeEnabled()

    // Same halt, in Auto, does disable them.
    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    for (const b of screen.getAllByRole('button', { name: 'Trade' })) {
      expect(b).toBeDisabled()
    }
  })
})

describe('Confidence slot', () => {
  it('carries the untested tag instead of a number, and only for the unvalidated row', () => {
    render(<App />)
    const recs = within(section('Recommended Trades'))

    expect(recs.getByText('Untested')).toBeInTheDocument()
    expect(recs.queryByText('Unvalidated')).not.toBeInTheDocument()
    expect(recs.getAllByText('Untested')).toHaveLength(1)
  })

  it('still shows an em dash where a scanner setup simply has no base rate yet', () => {
    render(<App />)
    const recs = within(section('Recommended Trades'))

    // rec-7: confidence null, unvalidated false. Distinct from rec-4.
    expect(recs.getAllByText('—')).toHaveLength(1)
  })
})

describe('Executions P&L column', () => {
  /** This reverses an earlier decision. The column used to be headed Status
   * and show P&L only when a row had one; it is now a P&L column and shows
   * nothing else. The status word is gone from the table body entirely —
   * see the em-dash test below for what that costs. */
  it('is headed P&L, not Status', () => {
    render(<App />)
    const executions = within(section('Recent Executions'))

    expect(executions.getByRole('columnheader', { name: 'P&L' })).toBeInTheDocument()
    expect(executions.queryByRole('columnheader', { name: 'Status' })).not.toBeInTheDocument()
  })

  it('carries a fill price per row', () => {
    render(<App />)
    const executions = within(section('Recent Executions'))

    expect(executions.getByRole('columnheader', { name: 'Price' })).toBeInTheDocument()
  })

  /** This feed is orders only. A deposit has no contract, no quantity and no
   * fill, so it is not an execution — the account's full ledger, cash
   * movements included, is on Activity (PRD.md §8.2). */
  it('leaves deposits and withdrawals off the executions feed', () => {
    render(<App />)
    const table = within(within(section('Recent Executions')).getByRole('table'))

    // The paper feed carries a +$5,000 deposit and a −$1,250 withdrawal.
    expect(table.queryByText('+$5,000.00')).not.toBeInTheDocument()
    expect(table.queryByText('−$1,250.00')).not.toBeInTheDocument()
    expect(table.queryByText('Deposit')).not.toBeInTheDocument()
    expect(table.queryByText('Withdrawal')).not.toBeInTheDocument()
  })

  it('stretches its rows so the panel ends level with Recommended Trades', () => {
    render(<App />)
    const table = within(section('Recent Executions')).getByRole('table')

    // The Dashboard sits this table beside another panel and the two have
    // to end level; Activity renders the same component in normal flow and
    // deliberately does not stretch. If these two ever get unified, one
    // page gets the wrong layout.
    expect(table.className).toMatch(/h-full/)
  })

  it('does not simply hide every row — the orders are still there', () => {
    render(<App />)
    const table = within(within(section('Recent Executions')).getByRole('table'))

    // Guards the filter against being too greedy: an empty table would
    // satisfy the assertions above just as well.
    expect(table.getAllByRole('row').length).toBeGreaterThan(1)
  })

  it('shows an em dash, not the status word, where a row has no P&L', () => {
    render(<App />)
    // Scoped to the table, not the section — the status filter's <option>
    // elements carry these same words.
    const table = within(within(section('Recent Executions')).getByRole('table'))

    expect(table.queryByText('Rejected')).not.toBeInTheDocument()
    expect(table.queryByText('Canceled')).not.toBeInTheDocument()
    expect(table.getAllByText('—').length).toBeGreaterThan(0)
  })

  /** The known cost of the column above, pinned so it stays a decision
   * rather than drifting into a surprise: on this page a rejected order and
   * a pending one are now visually identical. Activity is where a rejection
   * still states its reason inline (PRD.md §8.2). If that ever regresses,
   * rejections become invisible everywhere — CLAUDE.md rule 8. */
  it('leaves the reason on hover here, and inline on Activity', () => {
    render(<App />)
    const dash = within(within(section('Recent Executions')).getByRole('table'))
    expect(dash.queryByText(/breach|limit|exceed/i)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: 'Activity' }))
    const activity = within(within(section('Recent Activity')).getByRole('table'))
    expect(activity.getAllByText(/breach|limit|exceed/i).length).toBeGreaterThan(0)
  })
})

describe('Halt and Flatten read as different controls', () => {
  it('leaves Flatten enabled while positions are open and disables it after', () => {
    render(<App />)

    expect(screen.getByRole('button', { name: 'Flatten' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Flatten' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Flatten' }),
    )

    expect(screen.getByRole('button', { name: 'Flatten' })).toBeDisabled()
  })

  it('halting leaves Flatten available, because the positions are still open', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Auto' }))
    fireEvent.click(screen.getByRole('button', { name: 'Halt' }))

    expect(screen.getByRole('button', { name: 'Resume trading' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Flatten' })).toBeEnabled()
  })

  it('names the real position count in the flatten confirm', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('button', { name: 'Flatten' }))

    const dialog = screen.getByRole('alertdialog')
    const openCount = useUIStore.getState().openPositions.paper.length
    expect(within(dialog).getByText(new RegExp(`Closes all ${openCount} open positions`))).toBeInTheDocument()
  })
})
