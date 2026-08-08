import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS, activityStats } from '../lib/mockData'
import { formatPct, formatUsd } from '../lib/format'

const initialState = useUIStore.getState()

const PAPER = ACCOUNT_SNAPSHOTS.paper
const CASH = ACCOUNT_SNAPSHOTS.cash

/** Must match Activity.tsx's PAGE_SIZE. */
const PAGE_SIZE = 15
const pagesFor = (n: number) => Math.max(1, Math.ceil(n / PAGE_SIZE))

beforeEach(() => {
  // BrowserRouter reads window.location, and these tests navigate. Without
  // this, a test that ran after a navigation starts on the wrong page.
  window.history.pushState({}, '', '/')
  useUIStore.setState(initialState, true)
})

function section(heading: string): HTMLElement {
  return screen.getByRole('heading', { name: heading }).closest('section')!
}

function gotoActivity() {
  render(<App />)
  fireEvent.click(screen.getByRole('link', { name: 'Activity' }))
}

function positionRows(): HTMLElement[] {
  // Row 0 is the header row.
  return within(section('Open Positions')).getAllByRole('row').slice(1)
}

/** Activity is the account's full ledger, so cash movements belong here —
 * the Dashboard's Recent Executions is orders only (PRD.md §8.2). These
 * assertions used to live in the Dashboard suite; the rendering they cover
 * is unchanged, only where it is reachable. */
describe('Cash movements in the ledger', () => {
  it('shows the money moved on a deposit and a withdrawal, signed', () => {
    gotoActivity()
    const feed = within(within(section('Recent Activity')).getByRole('table'))

    expect(feed.getByText('+$5,000.00')).toBeInTheDocument()
    expect(feed.getByText('−$1,250.00')).toBeInTheDocument()
  })

  it('does not color a cash movement as if it were a gain or a loss', () => {
    gotoActivity()
    const deposit = within(within(section('Recent Activity')).getByRole('table')).getByText(
      '+$5,000.00',
    )

    // A deposit is money moved, not money made — DESIGN.md keeps
    // bullish/bearish for P&L.
    expect(deposit.className).not.toMatch(/text-bullish|text-bearish/)
  })

  it('does not stretch its rows — that is the Dashboard panel, not this page', () => {
    gotoActivity()
    const table = within(section('Recent Activity')).getByRole('table')

    expect(table.className).not.toMatch(/h-full/)
  })

  it('leaves the price column empty on a cash movement rather than showing zero', () => {
    gotoActivity()
    const depositRow = within(within(section('Recent Activity')).getByRole('table'))
      .getByText('+$5,000.00')
      .closest('tr')!

    // A zero would read as a free fill; there was no fill at all.
    expect(within(depositRow).getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('Header stats', () => {
  it('averages wins and losses separately, in dollars and percent', () => {
    gotoActivity()
    const stats = activityStats(PAPER.activity)

    const win = screen.getByRole('heading', { name: 'Activity' }).parentElement!
    expect(within(win).getByText(formatUsd(stats.avgWin!, { signed: true }))).toBeInTheDocument()
    expect(within(win).getByText(formatPct(stats.avgWinPct!, { signed: true }))).toBeInTheDocument()
    expect(within(win).getByText(formatUsd(stats.avgLoss!, { signed: true }))).toBeInTheDocument()
    expect(within(win).getByText(formatPct(stats.avgLossPct!, { signed: true }))).toBeInTheDocument()
  })

  it('sums lifetime P&L over realized trades only, excluding deposits', () => {
    gotoActivity()
    const stats = activityStats(PAPER.activity)

    // The paper feed carries a +$5,000 deposit and a −$1,250 withdrawal.
    // Neither is performance, so neither is in this number.
    expect(stats.lifetimePnl).toBeLessThan(5_000)
    expect(screen.getByText(formatUsd(stats.lifetimePnl, { signed: true }))).toBeInTheDocument()
  })

  it('colors a win bullish and a loss bearish, never error', () => {
    gotoActivity()
    const stats = activityStats(PAPER.activity)

    const avgWin = screen.getByText(formatUsd(stats.avgWin!, { signed: true })).closest('p')!
    const avgLoss = screen.getByText(formatUsd(stats.avgLoss!, { signed: true })).closest('p')!

    expect(avgWin.className).toMatch(/text-bullish/)
    expect(avgLoss.className).toMatch(/text-bearish/)
    // A losing trade is not a system failure (CLAUDE.md, DESIGN.md).
    expect(avgLoss.className).not.toMatch(/text-error/)
  })

  it('says how many trades each average is computed over', () => {
    gotoActivity()
    const stats = activityStats(PAPER.activity)

    expect(screen.getByText(`over ${stats.wins} winning trades`)).toBeInTheDocument()
    expect(screen.getByText(`over ${stats.losses} losing trades`)).toBeInTheDocument()
  })
})

describe('Open Positions', () => {
  it('renders one row per position in the active account, each with a Close', () => {
    gotoActivity()

    const rows = positionRows()
    expect(rows).toHaveLength(PAPER.positions.length)
    for (const row of rows) {
      expect(within(row).getByRole('button', { name: 'Close' })).toBeInTheDocument()
    }
  })

  it('states bid, ask and estimated proceeds when closing a long', () => {
    gotoActivity()

    const long = PAPER.positions.find((p) => p.direction === 'long')!
    const row = positionRows().find((r) => r.textContent?.includes(long.contract))!
    fireEvent.click(within(row).getByRole('button', { name: 'Close' }))

    const dialog = within(screen.getByRole('alertdialog'))
    // A long is sold to close at the bid, and that pays you.
    expect(dialog.getByText(/Sells/)).toBeInTheDocument()
    expect(dialog.getByText(new RegExp(`estimated proceeds`))).toBeInTheDocument()
    expect(
      dialog.getByText(new RegExp(formatUsd(long.bid * long.quantity * 100).replace(/\$/g, '\\$'))),
    ).toBeInTheDocument()
  })

  it('calls a short close a cost, not proceeds — it is a debit', () => {
    gotoActivity()

    const short = PAPER.positions.find((p) => p.direction === 'short')!
    const row = positionRows().find((r) => r.textContent?.includes(short.contract))!
    fireEvent.click(within(row).getByRole('button', { name: 'Close' }))

    const dialog = within(screen.getByRole('alertdialog'))
    expect(dialog.getByText(/Buys back/)).toBeInTheDocument()
    expect(dialog.getByText(/estimated cost to close/)).toBeInTheDocument()
    expect(dialog.queryByText(/estimated proceeds/)).not.toBeInTheDocument()
  })

  it('closes only that position, and does not halt the engine', () => {
    gotoActivity()

    const target = PAPER.positions[0]
    const row = positionRows().find((r) => r.textContent?.includes(target.contract))!
    fireEvent.click(within(row).getByRole('button', { name: 'Close' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Close position' }),
    )

    expect(positionRows()).toHaveLength(PAPER.positions.length - 1)
    // Close is to Flatten what Flatten is to Halt (CLAUDE.md rule 7).
    expect(useUIStore.getState().isHalted).toBe(false)
  })

  it('explains an empty book rather than showing a bare table', () => {
    gotoActivity()

    for (const p of PAPER.positions) {
      const row = positionRows().find((r) => r.textContent?.includes(p.contract))!
      fireEvent.click(within(row).getByRole('button', { name: 'Close' }))
      fireEvent.click(
        within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Close position' }),
      )
    }

    expect(within(section('Open Positions')).getByText(/No open positions in this account/)).toBeInTheDocument()
  })
})

describe('Recent Activity', () => {
  it('paginates the whole feed rather than scrolling it', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    expect(feed.getByText(`Page 1 of ${pagesFor(PAPER.activity.length)}`)).toBeInTheDocument()
    // One page of rows on screen, not all 50-odd.
    expect(feed.getAllByRole('row')).toHaveLength(PAGE_SIZE + 1)
  })

  it('advances to the next page', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    fireEvent.click(feed.getByRole('button', { name: 'Next' }))
    expect(feed.getByText(`Page 2 of ${pagesFor(PAPER.activity.length)}`)).toBeInTheDocument()
  })

  it('returns to the first page when the filter changes', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    fireEvent.click(feed.getByRole('button', { name: 'Next' }))
    expect(feed.getByText(/^Page 2 of/)).toBeInTheDocument()

    // Narrowing the filter while deep in the feed used to land on the last
    // page of the new result set, which reads as "no results".
    fireEvent.change(feed.getByRole('combobox', { name: 'Filter activity by status' }), {
      target: { value: 'filled' },
    })

    const filled = PAPER.activity.filter((a) => a.status === 'filled').length
    expect(feed.getByText(`Page 1 of ${pagesFor(filled)}`)).toBeInTheDocument()
  })
})

/** PRD.md §8.2 makes Activity the page of record for rejected orders, and
 * CLAUDE.md rule 8 says a rejection carries the rule that rejected it. A
 * reason reachable only by hovering is a reason that doesn't exist on a
 * screenshot, on a touch device, or to a keyboard. */
describe('Rejection reasons', () => {
  const reason = PAPER.activity.find((a) => a.status === 'rejected')!.rejectionReason!

  it('spells the rule out as visible text on Activity', () => {
    gotoActivity()

    expect(within(section('Recent Activity')).getByText(reason)).toBeInTheDocument()
  })

  it('leaves it on hover on the Dashboard, which is only a summary', () => {
    render(<App />)

    expect(within(section('Recent Executions')).queryByText(reason)).not.toBeInTheDocument()
  })
})

describe('The page is scoped to one account', () => {
  function switchToCash() {
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )
    fireEvent.click(screen.getByRole('link', { name: 'Activity' }))
  }

  it('shows the cash book, and none of paper’s positions, once Cash is live', () => {
    switchToCash()

    const rows = positionRows()
    expect(rows).toHaveLength(CASH.positions.length)

    const text = rows.map((r) => r.textContent).join(' ')
    for (const p of CASH.positions) expect(text).toContain(p.contract)
    for (const p of PAPER.positions) expect(text).not.toContain(p.contract)
  })

  it('recomputes the header stats off the cash feed', () => {
    switchToCash()

    const cash = activityStats(CASH.activity)
    const paper = activityStats(PAPER.activity)

    expect(screen.getByText(formatUsd(cash.lifetimePnl, { signed: true }))).toBeInTheDocument()
    expect(screen.queryByText(formatUsd(paper.lifetimePnl, { signed: true }))).not.toBeInTheDocument()
  })

  it('names the account, since the switch itself lives on the Dashboard', () => {
    switchToCash()

    // PRD.md §3: the read-only header badge is the stated mitigation for
    // account-scoped money on a page with no account switch.
    expect(screen.getByLabelText('Account: Cash')).toBeInTheDocument()
    expect(screen.getByText(/for your Cash account/)).toBeInTheDocument()
  })

  it('comes up in Paper on a cold start', () => {
    gotoActivity()

    expect(screen.getByLabelText('Account: Paper')).toBeInTheDocument()
    expect(screen.getByText(/for your Paper account/)).toBeInTheDocument()
  })
})
