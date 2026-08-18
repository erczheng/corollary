import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS, MARKET_TODAY, UNDERLYINGS, activityStats } from '../lib/mockData'
import { daysToExpiry, expiryUrgency } from '../lib/orders'
import { formatExpiry, formatPct, formatUsd } from '../lib/format'

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
  // `lastTickAt` seeded to *now*, so the page is past "connecting" and
  // rendering content rather than skeletons. It has to be now rather than
  // a fixed date, because the pill goes stale on elapsed time and a
  // hardcoded timestamp is permanently stale. Nothing has actually ticked
  // — the tick interval is far longer than these tests take — so every
  // fixture value is still the one the assertions expect.
  useUIStore.setState({ ...initialState, lastTickAt: new Date().toISOString() }, true)
})

function section(heading: string): HTMLElement {
  return screen.getByRole('heading', { name: heading }).closest('section')!
}

function gotoActivity() {
  render(<App />)
  fireEvent.click(screen.getByRole('link', { name: 'Activity' }))
}

/** Position rows only — not the header, and not the panel an expanded row
 * opens underneath itself, which is also a <tr>.
 *
 * Identified by the ⋯ menu rather than by the Close button: the ticket
 * inside the expanded panel has a "Close" mode button of its own, so that
 * would match the panel too. */
function positionRows(): HTMLElement[] {
  return within(section('Open Positions'))
    .getAllByRole('row')
    .filter((r) => within(r).queryByRole('button', { name: /^More actions for/ }) !== null)
}

function rowFor(contract: string): HTMLElement {
  return positionRows().find((r) => r.textContent?.includes(contract))!
}

/** Close opens the ticket; it does not submit. Getting from a row to a
 * closed position is: Close → Review close → confirm. */
function openTicket(contract: string, action = 'Close') {
  fireEvent.click(within(rowFor(contract)).getByRole('button', { name: action }))
}

function closeWholePosition(contract: string) {
  openTicket(contract)
  fireEvent.click(screen.getByRole('button', { name: 'Review close' }))
  fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Close position' }))
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
  /** Three peer figures, so three cards — the same treatment the Dashboard
   * gives its header stats. Each is its own labelled group, which is also
   * what makes these assertions scopeable. */
  it('stands each stat up as its own card', () => {
    gotoActivity()

    for (const label of ['Average win', 'Average loss', 'Lifetime P&L']) {
      expect(screen.getByRole('group', { name: label })).toBeInTheDocument()
    }
  })

  it('averages wins and losses separately, in dollars and percent', () => {
    gotoActivity()
    const stats = activityStats(PAPER.activity)

    const win = within(screen.getByRole('group', { name: 'Average win' }))
    expect(win.getByText(formatUsd(stats.avgWin!, { signed: true }))).toBeInTheDocument()
    expect(win.getByText(formatPct(stats.avgWinPct!, { signed: true }))).toBeInTheDocument()

    const loss = within(screen.getByRole('group', { name: 'Average loss' }))
    expect(loss.getByText(formatUsd(stats.avgLoss!, { signed: true }))).toBeInTheDocument()
    expect(loss.getByText(formatPct(stats.avgLossPct!, { signed: true }))).toBeInTheDocument()
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

  it('opens the ticket rather than submitting — Close alone changes nothing', () => {
    gotoActivity()

    const target = PAPER.positions[0]
    openTicket(target.contract)

    // The whole point of Close-as-order-button: the irreversible thing is
    // one confirm further along, not on the first click.
    expect(positionRows()).toHaveLength(PAPER.positions.length)
    expect(screen.getByRole('button', { name: 'Review close' })).toBeInTheDocument()
  })

  it('states bid, ask and estimated proceeds when closing a long', () => {
    gotoActivity()

    const long = PAPER.positions.find((p) => p.direction === 'long')!
    openTicket(long.contract)

    // A long is sold to close at the bid, and that pays you.
    expect(screen.getByText('Sell to close (STC)')).toBeInTheDocument()
    expect(screen.getByText('Estimated proceeds')).toBeInTheDocument()
    expect(screen.getByText(`${formatUsd(long.bid)} / ${formatUsd(long.ask)}`)).toBeInTheDocument()
    expect(screen.getByText(formatUsd(long.bid * long.quantity * 100))).toBeInTheDocument()
  })

  it('calls a short close a cost, not proceeds — it is a debit', () => {
    gotoActivity()

    const short = PAPER.positions.find((p) => p.direction === 'short')!
    openTicket(short.contract)

    expect(screen.getByText('Buy to close (BTC)')).toBeInTheDocument()
    expect(screen.getByText('Estimated cost')).toBeInTheDocument()
    expect(screen.queryByText('Estimated proceeds')).not.toBeInTheDocument()
  })

  it('closes only that position, and does not halt the engine', () => {
    gotoActivity()

    closeWholePosition(PAPER.positions[0].contract)

    expect(positionRows()).toHaveLength(PAPER.positions.length - 1)
    // Close is to Flatten what Flatten is to Halt (CLAUDE.md rule 7).
    expect(useUIStore.getState().isHalted).toBe(false)
  })

  it('explains an empty book rather than showing a bare table', () => {
    // Driven through the store rather than by closing four positions: a
    // multi-leg position can only take a limit order, so closing one
    // *works* an order rather than removing it, and the book cannot be
    // emptied from this page at all. That is covered separately below.
    useUIStore.setState({
      openPositions: { paper: [], cash: [] },
      workingOrders: { paper: [], cash: [] },
    })
    gotoActivity()

    expect(within(section('Open Positions')).getByText(/No open positions in this account/)).toBeInTheDocument()
  })

  it('fills a market close immediately, and only works a limit one', () => {
    gotoActivity()

    const single = PAPER.positions.find((p) => p.legs.length === 1)!
    closeWholePosition(single.contract)
    // Market: gone from the book, and in the ledger as filled.
    expect(positionRows().some((r) => r.textContent?.includes(single.contract))).toBe(false)

    const spread = PAPER.positions.find((p) => p.legs.length > 1)!
    closeWholePosition(spread.contract)
    // Limit, because a spread accepts nothing else: still open, now with
    // an order working against it.
    expect(positionRows().some((r) => r.textContent?.includes(spread.contract))).toBe(true)
    expect(useUIStore.getState().workingOrders.paper.some((o) => o.positionId === spread.id)).toBe(true)
  })
})

describe('Expanding a position', () => {
  const single = PAPER.positions.find((p) => p.legs.length === 1)!
  const spread = PAPER.positions.find((p) => p.legs.length > 1)!

  function expandRow(contract: string) {
    fireEvent.click(within(rowFor(contract)).getByRole('button', { name: /^Details for/ }))
  }

  it('opens a chart and a ticket in place, without covering the table', () => {
    gotoActivity()
    expandRow(single.contract)

    expect(screen.getByRole('group', { name: 'Chart view' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Ticket mode' })).toBeInTheDocument()
    // Nothing is modal — the rest of the book is still on screen.
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    expect(positionRows()).toHaveLength(PAPER.positions.length)
  })

  it('keeps one row open at a time', () => {
    gotoActivity()
    expandRow(single.contract)
    expandRow(spread.contract)

    expect(screen.getAllByRole('group', { name: 'Ticket mode' })).toHaveLength(1)
  })

  it('collapses again on a second click', () => {
    gotoActivity()
    expandRow(single.contract)
    expandRow(single.contract)

    expect(screen.queryByRole('group', { name: 'Ticket mode' })).not.toBeInTheDocument()
  })

  it('offers four order types on a single-leg position and limit alone on a spread', () => {
    gotoActivity()

    expandRow(single.contract)
    const singleTypes = within(screen.getByLabelText('Order type')).getAllByRole('option')
    expect(singleTypes.map((o) => o.textContent)).toEqual(['Market', 'Limit', 'Stop', 'Stop-Limit'])

    expandRow(spread.contract)
    const spreadTypes = within(screen.getByLabelText('Order type')).getAllByRole('option')
    expect(spreadTypes.map((o) => o.textContent)).toEqual(['Limit'])
    // And says why, rather than silently offering less.
    expect(screen.getByText(/limit orders only/i)).toBeInTheDocument()
  })

  it('reveals the stop field only for the order types that use one', () => {
    gotoActivity()
    expandRow(single.contract)

    expect(screen.queryByLabelText('Stop price')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Order type'), { target: { value: 'stop_limit' } })
    expect(screen.getByLabelText('Stop price')).toBeInTheDocument()
    expect(screen.getByLabelText('Limit price')).toBeInTheDocument()
  })

  it('switches the chart between value and payoff', () => {
    gotoActivity()
    expandRow(single.contract)

    const chart = within(screen.getByRole('group', { name: 'Chart view' }))
    expect(chart.getByRole('button', { name: 'Value since entry' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(chart.getByRole('button', { name: 'Payoff at expiry' }))
    // The payoff view carries the three numbers you'd otherwise read off
    // the curve by eye.
    expect(screen.getByText('Max loss')).toBeInTheDocument()
    expect(screen.getByText('Max profit')).toBeInTheDocument()
  })

  it('names the strategy managing the position, and the exits it applies', () => {
    gotoActivity()
    const managed = PAPER.positions.find((p) => p.strategyId !== null)!
    expandRow(managed.contract)

    expect(screen.getByText(/Managed by/)).toBeInTheDocument()
    expect(screen.getByText(/target 50%, stop 200%, 2 DTE/)).toBeInTheDocument()
  })

  it('states the underlying price and its day, which no column shows', () => {
    gotoActivity()
    expandRow(single.contract)

    // The Last column is the *contract's* mark — the price you close at.
    // The stock's own price and day, which is what actually moved the
    // position, appear nowhere else.
    const quote = UNDERLYINGS[single.symbol]
    expect(screen.getByText(`${single.symbol} underlying`)).toBeInTheDocument()
    expect(screen.getByText(formatUsd(quote.price))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`${formatPct(quote.changePct, { signed: true })} today`))).toBeInTheDocument()
  })

  it('charts the underlying, with previous close and every strike marked', () => {
    gotoActivity()
    expandRow(spread.contract)

    const chart = within(screen.getByRole('group', { name: 'Chart view' }))
    fireEvent.click(chart.getByRole('button', { name: 'Underlying' }))

    const quote = UNDERLYINGS[spread.symbol]
    expect(screen.getByText('Previous close')).toBeInTheDocument()
    expect(screen.getByText(formatUsd(quote.previousClose))).toBeInTheDocument()
    // Where the stock sits relative to the strikes is the question a
    // credit spread actually raises.
    const strikes = spread.legs
      .map((l) => `${l.side === 'short' ? 'Short' : 'Long'} ${formatUsd(l.strike)}`)
      .join(' · ')
    expect(screen.getByText(strikes)).toBeInTheDocument()
  })

  it('says where an already-attached exit is held', () => {
    gotoActivity()
    const withExit = PAPER.positions.find((p) => p.attachedExit !== null)!
    expandRow(withExit.contract)

    // A broker-held exit survives Corollary being down; a Corollary-held
    // one does not. The row is where that difference is visible.
    expect(screen.getByText(/exit held at broker/)).toBeInTheDocument()
  })
})

describe('Adding to a position', () => {
  it('calls adding to a short a sell to open, not a buy', () => {
    gotoActivity()
    const short = PAPER.positions.find((p) => p.direction === 'short')!

    fireEvent.click(within(rowFor(short.contract)).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to position' }))

    // A button reading "Buy" here would name the opposite of the order.
    expect(screen.getByText('Sell to open (STO)')).toBeInTheDocument()
  })

  it('grows the position and logs an opening fill with no P&L', () => {
    gotoActivity()
    const target = PAPER.positions[0]

    fireEvent.click(within(rowFor(target.contract)).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to position' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review add' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Add to position' }))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.quantity).toBe(target.quantity + 1)
    expect(useUIStore.getState().activity.paper[0].pnl).toBeNull()
  })

  it('shows the added risk as an estimate, never as an approval', () => {
    gotoActivity()
    const target = PAPER.positions[0]

    fireEvent.click(within(rowFor(target.contract)).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Add to position' }))

    // CLAUDE.md rule 4 — the engine enforces, the client never approves.
    expect(screen.getByText(/The risk manager decides; this is an estimate/)).toBeInTheDocument()
  })
})

describe('Attaching an exit', () => {
  const target = PAPER.positions.find((p) => p.strategyId !== null && p.legs.length === 1)!

  function openExitTicket() {
    fireEvent.click(within(rowFor(target.contract)).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Attach exit' }))
  }

  it('refuses a stop that is not clear of the take-profit', () => {
    gotoActivity()
    openExitTicket()

    fireEvent.change(screen.getByLabelText('Take profit'), { target: { value: '3.00' } })
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: '3.00' } })

    // Alpaca rejects an OCO whose stop is not a cent clear of its base.
    expect(screen.getByText(/at least \$0.01 below the take-profit/)).toBeInTheDocument()
  })

  it('attaches the exit and takes the position off its strategy', () => {
    gotoActivity()
    openExitTicket()

    fireEvent.change(screen.getByLabelText('Take profit'), { target: { value: '3.00' } })
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: '1.50' } })
    fireEvent.click(
      within(screen.getByRole('group', { name: 'Ticket mode' }).parentElement!)
        .getAllByRole('button', { name: 'Attach exit' })
        .at(-1)!,
    )
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Attach exit' }))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === target.id)!
    expect(after.attachedExit).not.toBeNull()
    // Manual replaces managed — one party responsible for closing it.
    expect(after.strategyId).toBeNull()
    expect(screen.getByText(/Manually managed/)).toBeInTheDocument()
  })
})

/** Until this section existed the terminal had no concept of "an order I
 * placed that hasn't happened yet" — everything filled on click, which
 * made Activity a record of the past rather than the ledger of record it
 * claims to be. */
describe('Working Orders', () => {
  it('lists the orders that are placed and unfilled', () => {
    gotoActivity()
    const working = within(section('Working Orders'))

    expect(working.getByText('TSLA $240 Put Nov 15')).toBeInTheDocument()
    expect(working.getByText('STC')).toBeInTheDocument()
    expect(working.getByText('1 working in Paper')).toBeInTheDocument()
  })

  it('shows an empty state on an account with none, pointing at where exits live', () => {
    gotoActivity()
    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )

    const working = within(section('Working Orders'))
    expect(working.getByText(/No working orders/)).toBeInTheDocument()
    // Attached exits are not duplicated in here, so the empty state has to
    // say where they actually are or it reads as "you have no exits".
    expect(working.getByText(/live on that position/)).toBeInTheDocument()
  })

  it('shows both prices on a stop-limit, which has two and they differ', () => {
    useUIStore.setState({
      workingOrders: {
        paper: [
          {
            id: 'wo-sl',
            positionId: 'pos-1',
            contractKey: null,
            contract: 'AAPL $230 Call Oct 17',
            side: 'STC',
            orderType: 'stop_limit',
            quantity: 1,
            limitPrice: 1.4,
            stopPrice: 1.5,
            timeInForce: 'gtc',
            placedAt: '2026-08-07T15:00:00Z',
            activityId: 'act-1',
          },
        ],
        cash: [],
      },
    })
    gotoActivity()

    expect(within(section('Working Orders')).getByText('$1.50 → $1.40')).toBeInTheDocument()
  })

  it('cancels an order and flips its ledger row rather than adding a second', () => {
    gotoActivity()
    const before = useUIStore.getState().activity.paper.length

    fireEvent.click(within(section('Working Orders')).getByRole('button', { name: 'Cancel' }))
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel order' }))

    const s = useUIStore.getState()
    expect(s.workingOrders.paper).toHaveLength(0)
    // One order, one life, one row in the ledger.
    expect(s.activity.paper).toHaveLength(before)
    expect(s.activity.paper.find((a) => a.id === 'act-1')!.status).toBe('canceled')
  })

  it('says cancelling an order is not closing the trade', () => {
    gotoActivity()
    fireEvent.click(within(section('Working Orders')).getByRole('button', { name: 'Cancel' }))

    expect(
      within(screen.getByRole('alertdialog')).getByText(/position itself is untouched/),
    ).toBeInTheDocument()
  })

  it('takes working orders with the position when it closes out', () => {
    gotoActivity()
    // wo-1 works against pos-2, which is single-leg and can close at market.
    const target = PAPER.positions.find((p) => p.id === 'pos-2')!
    closeWholePosition(target.contract)

    // An order against a position that no longer exists can never fill.
    expect(useUIStore.getState().workingOrders.paper.some((o) => o.positionId === 'pos-2')).toBe(false)
  })
})

/** Loading here is a real condition rather than a timer: before the first
 * price arrives there is nothing current to show, which is exactly the
 * state Phase 2 is in while the opening snapshot is in flight. */
describe('Loading states', () => {
  function beforeFirstPrice() {
    useUIStore.setState({ ...initialState, lastTickAt: null }, true)
    gotoActivity()
  }

  it('shows skeletons rather than an empty book before the first price', () => {
    beforeFirstPrice()

    // A stream that hasn't connected and an account with nothing in it
    // mean very different things at 9:31am, and a blank panel cannot tell
    // them apart.
    expect(screen.getByText('Loading open positions')).toBeInTheDocument()
    expect(screen.getByText('Loading working orders')).toBeInTheDocument()
    expect(screen.getByText('Loading activity')).toBeInTheDocument()
    expect(screen.queryByText(/No open positions/)).not.toBeInTheDocument()
  })

  it('announces loading to a screen reader without reading out the bars', () => {
    beforeFirstPrice()

    const busy = screen.getAllByRole('status').filter((s) => s.getAttribute('aria-busy') === 'true')
    expect(busy.length).toBeGreaterThan(0)
  })

  it('replaces the skeletons once a price has arrived', () => {
    gotoActivity()

    expect(screen.queryByText('Loading open positions')).not.toBeInTheDocument()
    expect(positionRows()).toHaveLength(PAPER.positions.length)
  })
})

describe('Live status', () => {
  it('reads Connecting until the first price', () => {
    useUIStore.setState({ ...initialState, lastTickAt: null }, true)
    gotoActivity()

    expect(screen.getByLabelText('Connecting to price stream')).toBeInTheDocument()
  })

  it('reports the time of the last price once streaming', () => {
    gotoActivity()

    // The question a positions screen has to answer continuously is "are
    // these numbers current". A Refresh button answered it once.
    expect(screen.getByLabelText(/^Live — last price /)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Refresh/ })).not.toBeInTheDocument()
  })

  it('stops claiming to be live once prices stop arriving', () => {
    useUIStore.setState(
      { ...initialState, lastTickAt: new Date(Date.now() - 60_000).toISOString() },
      true,
    )
    gotoActivity()

    // A badge reading "Live" beside a timestamp that stopped moving is a
    // status indicator lying about the one thing it exists for.
    expect(screen.getByLabelText(/^Stale — no price since /)).toBeInTheDocument()
    expect(screen.queryByLabelText(/^Live /)).not.toBeInTheDocument()
  })
})

describe('Expiry', () => {
  it('counts down the days on every position', () => {
    gotoActivity()

    for (const p of PAPER.positions) {
      const dte = daysToExpiry(p.expiry, MARKET_TODAY)
      const cell = within(rowFor(p.contract)).getByTitle(`Expires ${formatExpiry(p.expiry)}`)
      expect(cell).toHaveTextContent(dte < 0 ? 'Expired' : dte === 0 ? 'Today' : `${dte}d`)
    }
  })

  /* All three render paths need a fixture behind them or two of them are
   * branches nobody can ever see — the trap `pending` fell into before the
   * Activity page existed. */
  it('reaches the expiring-today and already-expired states', () => {
    gotoActivity()

    const today = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'today')!
    const expired = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'expired')!

    expect(within(rowFor(today.contract)).getByText('Today')).toBeInTheDocument()
    expect(within(rowFor(expired.contract)).getByText('Expired')).toBeInTheDocument()
  })

  it('flags an expired position in caution, the same as one nearly there', () => {
    gotoActivity()

    const expired = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'expired')!
    const cell = within(rowFor(expired.contract)).getByText('Expired').parentElement!
    expect(cell.className).toMatch(/text-caution/)
  })

  it('flags a position near expiry in caution, never in error', () => {
    gotoActivity()

    const near = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'near')!
    const cell = within(rowFor(near.contract)).getByTitle(`Expires ${formatExpiry(near.expiry)}`)
      .parentElement!

    // Running out of time is a deadline, not a system failure.
    expect(cell.className).toMatch(/text-caution/)
    expect(cell.className).not.toMatch(/text-error/)
  })

  it('leaves a position with room to run unflagged', () => {
    gotoActivity()

    const far = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'normal')!
    const cell = within(rowFor(far.contract)).getByTitle(`Expires ${formatExpiry(far.expiry)}`).parentElement!

    expect(cell.className).not.toMatch(/text-caution/)
  })
})

describe('The ticket knows the contract expires', () => {
  it('warns that a GTC order cannot outlive the contract', () => {
    gotoActivity()
    const near = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'near')!

    fireEvent.click(within(rowFor(near.contract)).getByRole('button', { name: 'Close' }))
    fireEvent.change(screen.getByLabelText('Time in force'), { target: { value: 'gtc' } })

    // "Good til canceled" is the one phrase on the ticket that reads like
    // a promise, and on a contract days from expiry it is a short one.
    expect(screen.getByText(/cannot outlive it/)).toBeInTheDocument()
  })

  it('says nothing of the sort for a day order', () => {
    gotoActivity()
    const near = PAPER.positions.find((p) => expiryUrgency(p, MARKET_TODAY) === 'near')!

    fireEvent.click(within(rowFor(near.contract)).getByRole('button', { name: 'Close' }))
    expect(screen.queryByText(/cannot outlive it/)).not.toBeInTheDocument()
  })

  it('names the expiry in the confirm, the last place you look', () => {
    gotoActivity()
    const target = PAPER.positions[0]

    fireEvent.click(within(rowFor(target.contract)).getByRole('button', { name: 'Close' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review close' }))

    expect(
      within(screen.getByRole('alertdialog')).getByText(
        new RegExp(`expiring ${formatExpiry(target.expiry)}`),
      ),
    ).toBeInTheDocument()
  })
})

describe('Keyboard', () => {
  it('opens the confirm on Enter rather than placing the order outright', () => {
    gotoActivity()
    const target = PAPER.positions[0]
    fireEvent.click(within(rowFor(target.contract)).getByRole('button', { name: 'Close' }))

    fireEvent.submit(screen.getByLabelText('Quantity').closest('form')!)

    // Enter reaches the dialog, not the broker. One keystroke should not
    // be able to close a position.
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    expect(useUIStore.getState().openPositions.paper).toHaveLength(PAPER.positions.length)
  })

  it('collapses the expanded row on Escape', () => {
    gotoActivity()
    fireEvent.click(within(rowFor(PAPER.positions[0].contract)).getByRole('button', { name: /^Details for/ }))
    expect(screen.getByRole('group', { name: 'Ticket mode' })).toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('group', { name: 'Ticket mode' })).not.toBeInTheDocument()
  })

  it('hands focus back to the menu button when the menu closes', () => {
    gotoActivity()
    const trigger = within(rowFor(PAPER.positions[0].contract)).getByRole('button', {
      name: /^More actions for/,
    })

    fireEvent.click(trigger)
    fireEvent.keyDown(window, { key: 'Escape' })

    // Otherwise focus lands on <body> and the next Tab restarts from the
    // top of the document.
    expect(document.activeElement).toBe(trigger)
  })
})

describe('Searching the ledger', () => {
  function search(text: string) {
    fireEvent.change(screen.getByLabelText('Search activity by symbol or contract'), {
      target: { value: text },
    })
  }

  it('narrows the feed to one symbol', () => {
    gotoActivity()
    search('AAPL')

    const rows = within(section('Recent Activity')).getAllByRole('row').slice(1)
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) expect(row.textContent).toContain('AAPL')
  })

  it('combines with the status filter rather than replacing it', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    search('SPY')
    fireEvent.change(feed.getByRole('combobox', { name: 'Filter activity by status' }), {
      target: { value: 'filled' },
    })

    for (const row of feed.getAllByRole('row').slice(1)) {
      expect(row.textContent).toContain('SPY')
    }
  })

  it('explains an empty result instead of looking like an empty account', () => {
    gotoActivity()
    search('NOTATICKER')

    // "No activity in this account yet" would be a lie with 50 rows behind
    // the search.
    expect(within(section('Recent Activity')).getByText(/Nothing matching/)).toBeInTheDocument()
  })

  it('returns to the first page when the search changes', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    fireEvent.click(feed.getByRole('button', { name: 'Next' }))
    expect(feed.getByText(/^Page 2 of/)).toBeInTheDocument()

    search('S')
    expect(feed.getByText(/^Page 1 of/)).toBeInTheDocument()
  })
})

describe('Reattaching to a strategy', () => {
  it('names the strategy that opened the position, not the active one', () => {
    gotoActivity()
    const detached = PAPER.positions.find((p) => p.strategyId === null)!

    fireEvent.click(within(rowFor(detached.contract)).getByRole('button', { name: /^More actions for/ }))

    // Reattaching to whichever strategy happens to be active now would
    // quietly move the position onto different exit rules.
    expect(screen.getByRole('button', { name: /^Reattach to / })).toBeInTheDocument()
  })

  it('restores the opening strategy and clears the manual exit', () => {
    gotoActivity()
    const detached = PAPER.positions.find((p) => p.strategyId === null)!

    fireEvent.click(within(rowFor(detached.contract)).getByRole('button', { name: /^More actions for/ }))
    fireEvent.click(screen.getByRole('button', { name: /^Reattach to / }))

    const after = useUIStore.getState().openPositions.paper.find((p) => p.id === detached.id)!
    expect(after.strategyId).toBe(detached.openedByStrategyId)
    expect(after.attachedExit).toBeNull()
  })
})

describe('Recent Activity columns', () => {
  it('runs Time, Asset, Action, P&L, Price, Qty, Status — in that order', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    expect(feed.getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Time',
      'Asset',
      'Action',
      'P&L',
      'Price',
      'Qty',
      'Status',
    ])
  })

  it('keeps the Dashboard on its narrower summary columns', () => {
    render(<App />)
    const executions = within(section('Recent Executions'))

    // The half-width panel merges Asset/Action and has no Status column.
    // Same component, same cell logic, different layout.
    expect(executions.getAllByRole('columnheader').map((h) => h.textContent)).toEqual([
      'Time',
      'Asset / Action',
      'Qty',
      'Price',
      'P&L',
    ])
  })

  it('gives the action its own column rather than prefixing the contract', () => {
    gotoActivity()

    const trade = PAPER.activity.find((a) => a.action === 'STC' && a.contract !== '—')!
    const row = within(section('Recent Activity'))
      .getAllByRole('row')
      .find((r) => r.textContent?.includes(trade.contract))!
    const cells = within(row).getAllByRole('cell')

    // Asset holds the contract alone; Action stands on its own beside it.
    expect(cells[1].textContent).toContain(trade.contract)
    expect(cells[1].textContent).not.toContain('STC')
    expect(cells[2].textContent).toBe('STC')
  })

  it('renders the status word, which the summary layout can only put on hover', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    fireEvent.change(feed.getByRole('combobox', { name: 'Filter activity by status' }), {
      target: { value: 'pending' },
    })

    const rows = feed.getAllByRole('row').slice(1)
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) {
      const cells = within(row).getAllByRole('cell')
      expect(cells[6].textContent).toBe('Pending')
    }
  })

  it('colors a rejection error and a pending order caution, never bearish', () => {
    gotoActivity()
    const feed = within(section('Recent Activity'))

    fireEvent.change(feed.getByRole('combobox', { name: 'Filter activity by status' }), {
      target: { value: 'rejected' },
    })

    const status = within(feed.getAllByRole('row')[1]).getAllByRole('cell')[6]
    // A rejected order is a rule outcome, not a losing position.
    expect(status.querySelector('span')!.className).toMatch(/text-error/)
    expect(status.querySelector('span')!.className).not.toMatch(/text-bearish/)
  })

  it('shows an em dash for the asset on a cash movement, and names it in Action', () => {
    gotoActivity()

    const deposit = PAPER.activity.find((a) => a.action === 'DEPOSIT')!
    const feed = within(section('Recent Activity'))
    const row = feed
      .getAllByRole('row')
      .find((r) => within(r).queryAllByRole('cell')[2]?.textContent === 'Deposit')!

    const cells = within(row).getAllByRole('cell')
    // A deposit has no contract and no fill price — em dashes, not zeros.
    expect(cells[1].textContent).toBe('—')
    expect(cells[4].textContent).toBe('—')
    expect(cells[3].textContent).toBe(formatUsd(deposit.amount!, { signed: true }))
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
  /** Switches from the toggle on Activity's own title line, rather than
   * going back to the Dashboard for it. Everything on this page is
   * account-scoped, so the control that scopes it lives here too. */
  function switchToCash() {
    gotoActivity()
    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )
  }

  it('carries the Paper/Cash toggle on the title line', () => {
    gotoActivity()

    const titleRow = screen.getByRole('heading', { name: 'Activity', level: 1 }).parentElement!
    expect(within(titleRow).getByRole('button', { name: 'Paper' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(within(titleRow).getByRole('button', { name: 'Cash' })).toBeInTheDocument()
  })

  it('still demands the confirm dialog before Cash goes live', () => {
    gotoActivity()

    // CLAUDE.md rule 5 — entering Cash is never one click, wherever the
    // switch happens to be rendered.
    fireEvent.click(screen.getByRole('button', { name: 'Cash' }))
    expect(useUIStore.getState().accountMode).toBe('paper')

    fireEvent.click(
      within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Switch to Cash' }),
    )
    expect(useUIStore.getState().accountMode).toBe('cash')
  })

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

  it('names the account in prose and in the header badge', () => {
    switchToCash()

    // The badge covers News, Markets, Research and the rest, which have no
    // switch of their own (PRD.md §3). It stays on Activity so the answer
    // to "whose money is this" is in the same place on every page.
    expect(screen.getByLabelText('Account: Cash')).toBeInTheDocument()
    expect(screen.getByText(/for your Cash account/)).toBeInTheDocument()
  })

  it('comes up in Paper on a cold start', () => {
    gotoActivity()

    expect(screen.getByLabelText('Account: Paper')).toBeInTheDocument()
    expect(screen.getByText(/for your Paper account/)).toBeInTheDocument()
  })
})
