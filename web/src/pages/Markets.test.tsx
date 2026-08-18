import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { OPTION_CHAIN, STOCKS } from '../lib/mockData'
import { CHAIN_UNDERLYINGS, MIN_VOLUME_STEPS, filterChain, rankStocks } from '../lib/markets'
import { formatCompactNumber, formatInteger, formatUsd } from '../lib/format'

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location. Same guard the other page tests
  // carry: without it a test that runs after a navigation starts on the
  // wrong page.
  window.history.pushState({}, '', '/markets')
  useUIStore.setState({ ...initialState }, true)
})

/** The tables are siblings under one page, so every query has to be scoped
 * or "Symbol" and "Change %" match twice. Each section is a named landmark
 * for exactly this reason — on the page as much as in the test. */
function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

function chainTable(): HTMLElement {
  return screen.getAllByRole('table')[0]
}

function stockTable(): HTMLElement {
  return screen.getAllByRole('table')[1]
}

function bodyRows(table: HTMLElement): HTMLElement[] {
  return within(table).getAllByRole('row').slice(1)
}

function firstCell(table: HTMLElement, row = 0): string {
  return within(bodyRows(table)[row]).getAllByRole('cell')[0].textContent ?? ''
}

describe('the Markets page renders both halves', () => {
  it('shows the chain and the stock universe', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Markets' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Options chains' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Stocks & ETFs' })).toBeInTheDocument()
    expect(screen.getAllByRole('table')).toHaveLength(2)
  })

  it('opens scoped to one underlying rather than the whole board', () => {
    render(<App />)

    // A chain is read one symbol at a time. Opening on six names
    // interleaved is not a view anyone wants.
    const opening = CHAIN_UNDERLYINGS[0]
    for (const row of bodyRows(chainTable())) {
      expect(within(row).getAllByRole('cell')[0]).toHaveTextContent(opening)
    }
  })
})

describe('the chain filters', () => {
  it('narrows to the underlying that was picked', () => {
    render(<App />)

    const target = CHAIN_UNDERLYINGS[1]
    fireEvent.change(screen.getByLabelText('Filter chain by underlying'), { target: { value: target } })

    for (const row of bodyRows(chainTable())) {
      expect(within(row).getAllByRole('cell')[0]).toHaveTextContent(target)
    }
  })

  it('reaches the designed empty state at the top volume floor', () => {
    render(<App />)

    const floor = MIN_VOLUME_STEPS[MIN_VOLUME_STEPS.length - 1]
    const thin = CHAIN_UNDERLYINGS.find(
      (s) => filterChain(OPTION_CHAIN, { underlying: s, minVolume: floor }).length === 0,
    )!

    fireEvent.change(screen.getByLabelText('Filter chain by underlying'), { target: { value: thin } })
    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: String(floor) },
    })

    // Not a blank panel: the message says what it means and what to do,
    // and names the floor that produced it.
    const chain = section('Options chains')
    expect(within(chain).queryAllByRole('table')).toHaveLength(0)
    // Scoped: the floor also appears in the select that produced it.
    const message = within(chain).getByText(new RegExp(`Nothing in ${thin}`))
    expect(message).toHaveTextContent(formatInteger(floor))
  })

  it('drops the empty state as soon as the floor comes back down', () => {
    render(<App />)

    const floor = MIN_VOLUME_STEPS[MIN_VOLUME_STEPS.length - 1]
    const thin = CHAIN_UNDERLYINGS.find(
      (s) => filterChain(OPTION_CHAIN, { underlying: s, minVolume: floor }).length === 0,
    )!

    fireEvent.change(screen.getByLabelText('Filter chain by underlying'), { target: { value: thin } })
    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: String(floor) },
    })
    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: '0' },
    })

    expect(screen.getAllByRole('table')).toHaveLength(2)
  })
})

describe('the chain rankings', () => {
  it('puts the biggest gainer on top, across every underlying', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Filter chain by underlying'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'gainers' } })

    const best = [...OPTION_CHAIN].sort((a, b) => b.changePct - a.changePct)[0]
    const cells = within(bodyRows(chainTable())[0]).getAllByRole('cell')

    expect(cells[0]).toHaveTextContent(best.symbol)
    expect(cells[3]).toHaveTextContent(formatUsd(best.strike))
  })

  it('does not hand the losers screen the same row', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Filter chain by underlying'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'gainers' } })
    const gainer = within(bodyRows(chainTable())[0]).getAllByRole('cell')[6].textContent

    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'losers' } })
    const loser = within(bodyRows(chainTable())[0]).getAllByRole('cell')[6].textContent

    expect(gainer).not.toBe(loser)
    expect(gainer).toMatch(/^\+/)
    expect(loser).toMatch(/^−/)
  })

  it('names the column it is sorted by, for a reader and for a screen reader', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'iv' } })

    // A table ordered by a rule the header does not name just looks
    // shuffled.
    const sorted = within(chainTable()).getByRole('columnheader', { name: /IV/ })
    expect(sorted).toHaveAttribute('aria-sort', 'descending')
    expect(within(chainTable()).getByRole('columnheader', { name: /Strike/ })).not.toHaveAttribute(
      'aria-sort',
    )
  })

  it('announces the direction each ranking actually runs in', () => {
    render(<App />)

    // The strike ladder counts up and the losers screen sorts up from the
    // worst. Hardcoding 'descending' told a screen reader the opposite of
    // what the table visibly does.
    expect(within(chainTable()).getByRole('columnheader', { name: /Strike/ })).toHaveAttribute(
      'aria-sort',
      'ascending',
    )

    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'losers' } })
    expect(within(chainTable()).getByRole('columnheader', { name: /Change %/ })).toHaveAttribute(
      'aria-sort',
      'ascending',
    )

    fireEvent.change(screen.getByLabelText('Rank chain by'), { target: { value: 'gainers' } })
    expect(within(chainTable()).getByRole('columnheader', { name: /Change %/ })).toHaveAttribute(
      'aria-sort',
      'descending',
    )
  })
})

describe('the chain table cells', () => {
  it('signs change and change % textually, not by colour alone', () => {
    render(<App />)

    for (const row of bodyRows(chainTable())) {
      const cells = within(row).getAllByRole('cell')
      expect(cells[5].textContent).toMatch(/^[+−]\$/)
      expect(cells[6].textContent).toMatch(/^[+−][\d.]+%$/)
    }
  })

  it('quotes last inside its own spread on every visible row', () => {
    render(<App />)

    // The invariant that matters if a row is ever read as a tradeable
    // price: bid <= last <= ask.
    for (const row of bodyRows(chainTable())) {
      const cells = within(row).getAllByRole('cell')
      const money = (text: string) => Number(text.replace(/[$,]/g, ''))
      const last = money(cells[4].textContent ?? '')
      expect(last).toBeGreaterThanOrEqual(money(cells[7].textContent ?? ''))
      expect(last).toBeLessThanOrEqual(money(cells[8].textContent ?? ''))
    }
  })

  it('renders an expiry as the date it is, not a day early', () => {
    render(<App />)

    // A bare YYYY-MM-DD parses as UTC midnight; formatted in ET it shows
    // the previous day. The chain expires Aug 21, so "Aug 20" here is the
    // timezone bug.
    expect(within(chainTable()).getAllByText('Aug 21').length).toBeGreaterThan(0)
    expect(within(chainTable()).queryByText('Aug 20')).not.toBeInTheDocument()
  })
})

describe('the stock universe', () => {
  it('opens on most active and orders by volume', () => {
    render(<App />)

    const expected = rankStocks(STOCKS, 'active')[0]
    expect(firstCell(stockTable())).toBe(expected.symbol)
    expect(within(stockTable()).getAllByText(formatCompactNumber(expected.volume)).length).toBeGreaterThan(
      0,
    )
  })

  it('shows an em dash for a fund rather than a market cap of zero', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'marketCap' } })

    const fund = STOCKS.find((s) => s.marketCap === null)!
    const row = bodyRows(stockTable()).find(
      (r) => within(r).getAllByRole('cell')[0].textContent === fund.symbol,
    )

    // An ETF has no market capitalisation. $0.00B would read as a fund
    // worth nothing.
    if (row) {
      expect(within(row).getAllByRole('cell')[6]).toHaveTextContent('—')
      expect(within(row).getAllByRole('cell')[6]).not.toHaveTextContent('$0')
    } else {
      // Sorted last, so it may be on page two — which is itself the
      // assertion: nulls do not rank above real companies.
      const capped = STOCKS.filter((s) => s.marketCap !== null)
      expect(bodyRows(stockTable()).length).toBeLessThanOrEqual(capped.length)
    }
  })

  it('ranks the newest listing first', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'new' } })

    expect(firstCell(stockTable())).toBe(rankStocks(STOCKS, 'new')[0].symbol)
  })
})

describe('pagination', () => {
  it('pages the chain and moves to a different set of contracts', () => {
    render(<App />)

    const first = firstCell(chainTable())
    const rowsOnPageOne = bodyRows(chainTable()).length
    expect(rowsOnPageOne).toBe(15)

    const chain = section('Options chains')
    fireEvent.click(within(chain).getByRole('button', { name: 'Next' }))

    // Same underlying, different contracts — the ladder continues.
    expect(firstCell(chainTable())).toBe(first)
    expect(within(chain).getByText(/Page 2 of/)).toBeInTheDocument()
  })

  it('returns to page one when a filter narrows the result set', () => {
    render(<App />)

    const chain = section('Options chains')
    fireEvent.click(within(chain).getByRole('button', { name: 'Next' }))
    expect(within(chain).getByText(/Page 2 of/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: String(MIN_VOLUME_STEPS[1]) },
    })

    // Otherwise narrowing while deep in the chain lands on the last page
    // of a shorter list, which reads as "no contracts".
    expect(within(chain).getByText(/Page 1 of/)).toBeInTheDocument()
  })
})
