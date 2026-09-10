import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS } from '../lib/mockData'
import { cashTransfers, netTransfers, positionsValue, totalEquity } from '../lib/account'
import { formatUsd } from '../lib/format'

const initialState = useUIStore.getState()
const PAPER = ACCOUNT_SNAPSHOTS.paper
const CASH = ACCOUNT_SNAPSHOTS.cash

beforeEach(() => {
  window.history.pushState({}, '', '/account')
  // Seeded to *now* so the page is past "connecting" and rendering figures
  // rather than skeletons. Nothing has actually ticked — the interval is far
  // longer than these tests take — so every fixture value still holds.
  useUIStore.setState({ ...initialState, lastTickAt: new Date().toISOString() }, true)
})

function panel(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

describe('Account balances', () => {
  it('shows cash, buying power and options buying power', () => {
    render(<App />)

    expect(within(screen.getByRole('group', { name: 'Cash' })).getByText(formatUsd(PAPER.cash))).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Buying power' })).getByText(formatUsd(PAPER.buyingPower)),
    ).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Options buying power' })).getByText(
        formatUsd(PAPER.optionsBuyingPower),
      ),
    ).toBeInTheDocument()
  })

  /** Options are not marginable, so this note is not decoration: sizing an
   * option order against equity buying power overstates capacity by 2×. */
  it('says options are not marginable', () => {
    render(<App />)
    expect(screen.getByText('Options are not marginable')).toBeInTheDocument()
  })

  it('describes paper buying power as margin and the cash account as having none', () => {
    render(<App />)
    expect(screen.getByText(/Margin account/)).toBeInTheDocument()

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    expect(screen.getByText(/no margin/)).toBeInTheDocument()
    expect(screen.queryByText(/Margin account/)).not.toBeInTheDocument()
  })

  /** Rendering one account's money while the other is selected is the exact
   * failure CLAUDE.md calls out for account-scoped state. */
  it('shows the other account’s money once Cash is selected', () => {
    render(<App />)
    act(() => useUIStore.setState({ accountMode: 'cash' }))

    expect(within(screen.getByRole('group', { name: 'Cash' })).getByText(formatUsd(CASH.cash))).toBeInTheDocument()
    expect(
      within(screen.getByRole('group', { name: 'Cash' })).queryByText(formatUsd(PAPER.cash)),
    ).not.toBeInTheDocument()
  })

  /** PRD.md §2: the switch belongs on any page whose entire contents are
   * account-scoped, which this page is more completely than any other. */
  it('carries the Paper/Cash switch, not just the header badge', () => {
    render(<App />)
    const main = screen.getByRole('main')
    expect(within(main).getByRole('button', { name: 'Paper' })).toBeInTheDocument()
    expect(within(main).getByRole('button', { name: 'Cash' })).toBeInTheDocument()
  })
})

/** The settled/unsettled panel was removed with the fields behind it —
 * Alpaca's account endpoint publishes no settlement breakdown, so the panel
 * was three derived numbers presented as broker facts. This asserts it stays
 * gone, since the page it was on is otherwise entirely real values and a
 * plausible-looking reconstruction is the one thing that would not be. */
describe('settlement', () => {
  it('claims no settlement breakdown Alpaca does not supply', () => {
    render(<App />)
    expect(screen.queryByRole('region', { name: 'Settled vs unsettled' })).not.toBeInTheDocument()

    act(() => useUIStore.setState({ accountMode: 'cash' }))
    expect(screen.queryByRole('region', { name: 'Settled vs unsettled' })).not.toBeInTheDocument()
    expect(screen.queryByText(/good-faith violation/)).not.toBeInTheDocument()
  })
})

describe('total equity', () => {
  it('is cash plus the market value of open positions', () => {
    render(<App />)
    const p = panel('Total equity')

    expect(within(p).getByText(formatUsd(positionsValue(PAPER.positions)))).toBeInTheDocument()
    expect(
      within(p).getByText(formatUsd(totalEquity(PAPER.cash, PAPER.positions))),
    ).toBeInTheDocument()
  })

  /** Equity sums the *full* cash balance, some of which may not be spendable.
   * The page has to say which reading it took, and point at the figure that
   * answers the other question, rather than leaving both implied. */
  it('says it sums the full balance and names buying power as the spendable one', () => {
    render(<App />)
    const p = panel('Total equity')

    expect(within(p).getByText(/full balance/)).toBeInTheDocument()
    expect(within(p).getByText(/buying power/)).toBeInTheDocument()
  })

  /** Position value is marked from the stream and cash is not. Before the
   * first price there is no honest equity figure — showing cost basis would
   * be wrong by the whole unrealized P&L, and showing zero reads as a flat
   * book. */
  it('skeletons the marked figures until the first price arrives', () => {
    useUIStore.setState({ lastTickAt: null })
    render(<App />)

    const p = panel('Total equity')
    expect(within(p).getByText('Computing equity')).toBeInTheDocument()
    expect(within(p).getByText('Marking open positions')).toBeInTheDocument()
    expect(
      within(p).queryByText(formatUsd(totalEquity(PAPER.cash, PAPER.positions))),
    ).not.toBeInTheDocument()
  })

  it('carries the live pill on the panel that actually moves', () => {
    render(<App />)
    expect(within(panel('Total equity')).getByRole('status')).toBeInTheDocument()
  })
})

describe('cash transfers', () => {
  it('lists deposits and withdrawals and excludes orders', () => {
    render(<App />)
    const table = within(panel('Cash transfers')).getByRole('table')

    const transfers = cashTransfers(PAPER.activity)
    expect(within(table).getAllByRole('row')).toHaveLength(transfers.length + 2) // header + footer
    expect(within(table).getAllByText('Deposit').length).toBeGreaterThan(0)
  })

  /** Sign is textual as well as coloured — colour alone fails in grayscale
   * and for colourblind readers. */
  it('signs every amount explicitly', () => {
    render(<App />)
    const table = within(panel('Cash transfers')).getByRole('table')

    for (const t of cashTransfers(PAPER.activity)) {
      expect(within(table).getAllByText(formatUsd(t.amount ?? 0, { signed: true })).length).toBeGreaterThan(0)
    }
  })

  it('nets the transfers in a footer row', () => {
    render(<App />)
    const table = within(panel('Cash transfers')).getByRole('table')

    expect(within(table).getByText('Net transferred')).toBeInTheDocument()
    expect(
      within(table).getByText(formatUsd(netTransfers(cashTransfers(PAPER.activity)), { signed: true })),
    ).toBeInTheDocument()
  })

  it('links out to Alpaca rather than offering a transfer form', () => {
    render(<App />)
    const link = within(panel('Cash transfers')).getByRole('link', { name: /Deposit or withdraw at Alpaca/ })
    expect(link).toHaveAttribute('href', 'https://app.alpaca.markets/')
  })

  it('says paper transfers are simulated', () => {
    render(<App />)
    expect(screen.getByText(/Paper transfers are simulated/)).toBeInTheDocument()
  })

  /** A designed empty state, not a blank table. No transfers in a book that
   * has traded means something specific — the balance got here through P&L. */
  it('explains an empty ledger instead of rendering an empty table', () => {
    useUIStore.setState({
      activity: { paper: PAPER.activity.filter((a) => a.amount === null), cash: CASH.activity },
    })
    render(<App />)

    const p = panel('Cash transfers')
    expect(within(p).queryByRole('table')).not.toBeInTheDocument()
    expect(within(p).getByText(/balance has moved through trading alone/)).toBeInTheDocument()
  })
})
