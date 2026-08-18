import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { STOCKS, sliceRange } from '../lib/mockData'
import { CHAIN_UNDERLYINGS, MIN_VOLUME_STEPS, filterChain, relativeVolume } from '../lib/markets'
import { formatInteger } from '../lib/format'

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location. Same guard the other page tests
  // carry: without it a test that runs after a navigation starts on the
  // wrong page.
  window.history.pushState({}, '', '/markets')
  useUIStore.setState({ ...initialState }, true)
})

/** Each section is a named landmark, so the two tables can be told apart —
 * on the page as much as in the test, where "Symbol" and "Change %" would
 * otherwise match twice. */
function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

function chainTable(): HTMLElement {
  return within(section('Options chains')).getByRole('table')
}

function stockTable(): HTMLElement {
  return within(section('Stocks & ETFs')).getByRole('table')
}

function bodyRows(table: HTMLElement): HTMLElement[] {
  // The expanded ticket is its own row with one full-width cell, so it is
  // not a data row and would otherwise skew every index below.
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .filter((r) => within(r).queryAllByRole('cell').length > 1)
}

function cells(table: HTMLElement, row = 0): HTMLElement[] {
  return within(bodyRows(table)[row]).getAllByRole('cell')
}

function column(table: HTMLElement, index: number): string[] {
  return bodyRows(table).map((r) => within(r).getAllByRole('cell')[index].textContent ?? '')
}

function numbers(table: HTMLElement, index: number): number[] {
  return column(table, index).map((t) => Number(t.replace(/[$,×%+]/g, '').replace('−', '-')))
}

function searchBox(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Search underlying' })
}

describe('the Markets page renders both halves, live', () => {
  it('shows the chain and the stock universe', () => {
    render(<App />)

    expect(screen.getByRole('heading', { name: 'Markets' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Options chains' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Stocks & ETFs' })).toBeInTheDocument()
  })

  it('takes its first snapshot on mount rather than showing skeletons for two seconds', () => {
    render(<App />)

    // Loading is a real condition — but a page that sat blank for a full
    // poll interval on every visit would be reporting a connection problem
    // it does not have.
    expect(useUIStore.getState().lastPollAt).not.toBeNull()
    expect(bodyRows(chainTable())).toHaveLength(15)
  })

  it('says it is live once prices are arriving', () => {
    render(<App />)
    expect(screen.getByRole('status')).toHaveAccessibleName(/^Live/)
  })

  it('moves the chain and the stocks when a snapshot lands', () => {
    render(<App />)

    const before = column(chainTable(), 4)
    const stocksBefore = column(stockTable(), 2)

    act(() => {
      useUIStore.getState().pollMarkets(2_000)
    })

    expect(column(chainTable(), 4)).not.toEqual(before)
    expect(column(stockTable(), 2)).not.toEqual(stocksBefore)
  })

  it('opens scoped to one underlying rather than the whole board', () => {
    render(<App />)

    for (const symbol of column(chainTable(), 0)) {
      expect(symbol).toBe(CHAIN_UNDERLYINGS[0])
    }
  })
})

describe('the underlying search', () => {
  it('filters the list as you type and commits on Enter', () => {
    render(<App />)

    fireEvent.change(searchBox(), { target: { value: 'qq' } })
    expect(within(screen.getByRole('listbox')).getAllByRole('option').map((o) => o.textContent)).toEqual(
      ['QQQ'],
    )

    fireEvent.keyDown(searchBox(), { key: 'Enter' })

    for (const symbol of column(chainTable(), 0)) expect(symbol).toBe('QQQ')
  })

  it('commits on click', () => {
    render(<App />)

    fireEvent.change(searchBox(), { target: { value: 'msf' } })
    fireEvent.mouseDown(within(screen.getByRole('listbox')).getByRole('option', { name: 'MSFT' }))

    for (const symbol of column(chainTable(), 0)) expect(symbol).toBe('MSFT')
  })

  it('walks the list with the arrow keys', () => {
    render(<App />)

    fireEvent.focus(searchBox())
    // Opens on "All underlyings"; one step down is the first symbol.
    fireEvent.keyDown(searchBox(), { key: 'ArrowDown' })
    fireEvent.keyDown(searchBox(), { key: 'Enter' })

    expect(new Set(column(chainTable(), 0)).size).toBeGreaterThanOrEqual(1)
  })

  it('finds nothing for a symbol that is not listed, rather than a near match', () => {
    render(<App />)

    // "APPL" is the classic typo. Silently matching AAPL would filter the
    // chain to a symbol nobody asked for.
    fireEvent.change(searchBox(), { target: { value: 'APPL' } })

    // Scoped to the listbox: every native <select> on the page also
    // publishes options, and there are three of them.
    expect(within(screen.getByRole('listbox')).queryAllByRole('option')).toHaveLength(0)
    expect(screen.getByText(/No listed chain matches/)).toBeInTheDocument()
  })

  it('can go back to every underlying at once', () => {
    render(<App />)

    fireEvent.focus(searchBox())
    fireEvent.mouseDown(
      within(screen.getByRole('listbox')).getByRole('option', { name: 'All underlyings' }),
    )

    expect(within(section('Options chains')).getByText(/180 of 180 contracts/)).toBeInTheDocument()
  })
})

describe('sorting by clicking a column', () => {
  it('sorts the chain descending on the first click and flips on the second', () => {
    render(<App />)

    const volume = () => within(chainTable()).getByRole('button', { name: /Volume/ })

    fireEvent.click(volume())
    const down = numbers(chainTable(), 9)
    expect(down).toEqual([...down].sort((a, b) => b - a))

    fireEvent.click(volume())
    const up = numbers(chainTable(), 9)
    expect(up).toEqual([...up].sort((a, b) => a - b))
  })

  it('announces the direction it actually sorted in', () => {
    render(<App />)

    const header = () => within(chainTable()).getByRole('columnheader', { name: /Volume/ })

    fireEvent.click(within(chainTable()).getByRole('button', { name: /Volume/ }))
    expect(header()).toHaveAttribute('aria-sort', 'descending')

    fireEvent.click(within(chainTable()).getByRole('button', { name: /Volume/ }))
    expect(header()).toHaveAttribute('aria-sort', 'ascending')
  })

  it('leaves the identity columns unclickable', () => {
    render(<App />)

    // Sorting by Type splits a ladder into two blocks that no longer read
    // as a chain, and Strike across three expirations interleaves ladders
    // that do not exist.
    for (const name of ['Symbol', 'Exp', 'Type', 'Strike']) {
      expect(within(chainTable()).queryByRole('button', { name })).not.toBeInTheDocument()
    }
  })

  it('sorts the stock table too, market cap included', () => {
    render(<App />)

    const button = () => within(stockTable()).getByRole('button', { name: /Market cap/ })

    fireEvent.click(button())
    expect(within(stockTable()).getByRole('columnheader', { name: /Market cap/ })).toHaveAttribute(
      'aria-sort',
      'descending',
    )

    // Ascending is the case that catches a null coerced to zero — it would
    // sort a fund to the top.
    fireEvent.click(button())
    expect(cells(stockTable())[7].textContent).not.toBe('—')
  })

  it('says the sort is custom once a header has left the named screens', () => {
    render(<App />)

    const rank = screen.getByLabelText('Rank chain by') as HTMLSelectElement
    expect(rank.value).toBe('strike')

    fireEvent.click(within(chainTable()).getByRole('button', { name: /^Bid/ }))

    // Leaving "Strike ladder" selected would have the dropdown claiming a
    // ranking the table is no longer in.
    expect(rank.value).toBe('custom')
  })

  it('lights the matching preset back up when a click lands on one', () => {
    render(<App />)
    const rank = () => screen.getByLabelText('Rank chain by') as HTMLSelectElement

    // One click on Change % is exactly the "Top gainers" screen; a second
    // is "Top losers".
    fireEvent.click(within(chainTable()).getByRole('button', { name: /Change %/ }))
    expect(rank().value).toBe('gainers')

    fireEvent.click(within(chainTable()).getByRole('button', { name: /Change %/ }))
    expect(rank().value).toBe('losers')
  })
})

describe('trending now', () => {
  it('offers trending and no longer offers new listings', () => {
    render(<App />)

    const rank = screen.getByLabelText('Rank stocks by')
    expect(within(rank).getByText('Trending now')).toBeInTheDocument()
    expect(within(rank).queryByText('New listings')).not.toBeInTheDocument()
  })

  it('ranks by relative volume, which is a different table from most active', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'trending' } })
    const trending = cells(stockTable())[0].textContent

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'active' } })
    const active = cells(stockTable())[0].textContent

    // Raw volume finds the same mega caps every session; relative volume
    // finds the name having an unusual day.
    expect(trending).not.toBe(active)
    expect(trending).toBe([...STOCKS].sort((a, b) => relativeVolume(b) - relativeVolume(a))[0].symbol)
  })

  it('shows the multiple in its own column, and has dropped the listing date', () => {
    render(<App />)

    expect(within(stockTable()).getByRole('columnheader', { name: /Rel vol/ })).toBeInTheDocument()
    expect(
      within(stockTable()).queryByRole('columnheader', { name: /Listed/ }),
    ).not.toBeInTheDocument()
    expect(cells(stockTable())[6].textContent).toMatch(/^\d+\.\d{2}×$/)
  })
})

describe('trading a contract from the chain', () => {
  function openTicket(): void {
    fireEvent.click(within(chainTable()).getAllByRole('button', { name: /^Trade/ })[0])
  }

  it('expands a ticket under the row it belongs to', () => {
    render(<App />)
    openTicket()

    // The side selector and the submit are separate controls with
    // separate names — one chooses, one places.
    expect(screen.getByRole('button', { name: 'Buy to open (BTO)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sell to open (STO)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review buy to open' })).toBeInTheDocument()
    expect(screen.getByLabelText('Contracts')).toBeInTheDocument()
  })

  it('states a cost for a buy and a credit for a sell', () => {
    render(<App />)
    openTicket()

    expect(screen.getByText('Estimated cost')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Sell to open (STO)' }))
    expect(screen.getByText('Estimated credit')).toBeInTheDocument()
  })

  it('refuses to quote a maximum loss on a short', () => {
    render(<App />)
    openTicket()
    fireEvent.click(screen.getByRole('button', { name: 'Sell to open (STO)' }))

    // A confident wrong number under the word "risk" is worse than an
    // honest absence — the engine sizes this against a ±2σ stress loss.
    expect(screen.getByText('Undefined')).toBeInTheDocument()
    expect(screen.getByText(/undefined-risk position/)).toBeInTheDocument()
  })

  it('opens a position after a confirm that names the consequence', () => {
    render(<App />)
    openTicket()

    fireEvent.change(screen.getByLabelText('Order type'), { target: { value: 'market' } })

    const before = useUIStore.getState().openPositions.paper.length
    fireEvent.click(screen.getByRole('button', { name: 'Review buy to open' }))

    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toHaveTextContent(/Buying 1 contract/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Buy to open (BTO)' }))

    expect(useUIStore.getState().openPositions.paper).toHaveLength(before + 1)
    // And the ticket closes behind it, rather than sitting open over a
    // position that now exists.
    expect(screen.queryByLabelText('Contracts')).not.toBeInTheDocument()
  })

  it('will not let a halted engine open a new position', () => {
    render(<App />)
    act(() => {
      useUIStore.getState().halt()
    })
    openTicket()

    // Halt stops new entries. That is the one thing halt is for.
    expect(screen.getByRole('button', { name: 'Review buy to open' })).toBeDisabled()
    expect(screen.getByText(/Trading is halted/)).toBeInTheDocument()
  })
})

describe('the chain filters', () => {
  it('reaches the designed empty state at the top volume floor', () => {
    render(<App />)

    const floor = MIN_VOLUME_STEPS[MIN_VOLUME_STEPS.length - 1]
    const chain = useUIStore.getState().chain
    const thin = CHAIN_UNDERLYINGS.find(
      (s) => filterChain(chain, { underlying: s, minVolume: floor }).length === 0,
    )!

    fireEvent.change(searchBox(), { target: { value: thin } })
    fireEvent.keyDown(searchBox(), { key: 'Enter' })
    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: String(floor) },
    })

    const region = section('Options chains')
    expect(within(region).queryAllByRole('table')).toHaveLength(0)
    expect(within(region).getByText(new RegExp(`Nothing in ${thin}`))).toHaveTextContent(
      formatInteger(floor),
    )
  })
})

describe('the stock table cells', () => {
  it('signs change and change % textually, not by colour alone', () => {
    render(<App />)

    for (const row of bodyRows(stockTable())) {
      const c = within(row).getAllByRole('cell')
      expect(c[3].textContent).toMatch(/^[+−]\$/)
      expect(c[4].textContent).toMatch(/^[+−][\d.]+%$/)
    }
  })

  it('shows an em dash for a fund rather than a market cap of zero', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Rank stocks by'), { target: { value: 'marketCap' } })

    const funds = STOCKS.filter((s) => s.marketCap === null).map((s) => s.symbol)
    for (const row of bodyRows(stockTable())) {
      const c = within(row).getAllByRole('cell')
      if (funds.includes(c[0].textContent ?? '')) {
        expect(c[7]).toHaveTextContent('—')
        expect(c[7]).not.toHaveTextContent('$0')
      }
    }
  })

  it('quotes the same price the rest of the app has for that stock', () => {
    render(<App />)

    // One price per symbol. A stock carrying its own copy is how the
    // Markets table and an Activity row end up disagreeing about AAPL.
    const quotes = useUIStore.getState().underlyings
    for (const row of bodyRows(stockTable())) {
      const c = within(row).getAllByRole('cell')
      const symbol = c[0].textContent ?? ''
      const shown = Number((c[2].textContent ?? '').replace(/[$,]/g, ''))
      expect(shown).toBeCloseTo(quotes[symbol].price, 2)
    }
  })
})

describe('pagination', () => {
  it('pages the chain without leaving the underlying', () => {
    render(<App />)

    const region = section('Options chains')
    const first = cells(chainTable())[0].textContent
    expect(bodyRows(chainTable())).toHaveLength(15)

    fireEvent.click(within(region).getByRole('button', { name: 'Next' }))

    expect(cells(chainTable())[0].textContent).toBe(first)
    expect(within(region).getByText(/Page 2 of/)).toBeInTheDocument()
  })

  it('returns to page one when a filter narrows the result set', () => {
    render(<App />)

    const region = section('Options chains')
    fireEvent.click(within(region).getByRole('button', { name: 'Next' }))
    expect(within(region).getByText(/Page 2 of/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Filter chain by minimum volume'), {
      target: { value: String(MIN_VOLUME_STEPS[1]) },
    })

    expect(within(region).getByText(/Page 1 of/)).toBeInTheDocument()
  })
})

describe('the underlying chart in the ticket', () => {
  function openTicket(): void {
    fireEvent.click(within(chainTable()).getAllByRole('button', { name: /^Trade/ })[0])
  }

  it('shows the stock behind the contract, with its strike named', () => {
    render(<App />)
    openTicket()

    // An option ticket without the underlying asks you to price a
    // derivative with the derivative hidden.
    expect(screen.getByRole('group', { name: 'Chart range' })).toBeInTheDocument()
    expect(screen.getByText(/is (in|out of) the money against the/)).toBeInTheDocument()
  })

  it('offers every range, and they are not all the same chart', () => {
    render(<App />)
    openTicket()

    const group = screen.getByRole('group', { name: 'Chart range' })
    const ranges = within(group)
      .getAllByRole('button')
      .map((b) => b.textContent)
    expect(ranges).toEqual(['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All'])

    // The quote carries a year of closes precisely so these differ. At a
    // quarter, 3M / YTD / 1Y / All redrew an identical chart — four
    // buttons pretending to be a control.
    const quote = useUIStore.getState().underlyings[CHAIN_UNDERLYINGS[0]]
    const lengths = new Set(
      (['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All'] as const).map(
        (r) => sliceRange(quote.history, r).length,
      ),
    )
    expect(lengths.size).toBe(7)
  })

  it('reports the move over the window on screen, not over the day', () => {
    render(<App />)
    openTicket()

    const group = screen.getByRole('group', { name: 'Chart range' })
    expect(screen.getByText(/over 3M/)).toBeInTheDocument()

    fireEvent.click(within(group).getByRole('button', { name: '1W' }))

    // A range control that redraws the axis but leaves a daily figure
    // beside it is reporting on a chart nobody is looking at.
    expect(screen.getByText(/over 1W/)).toBeInTheDocument()
    expect(screen.queryByText(/over 3M/)).not.toBeInTheDocument()
  })

  it('marks the range that is showing, for a screen reader too', () => {
    render(<App />)
    openTicket()

    const group = screen.getByRole('group', { name: 'Chart range' })
    expect(within(group).getByRole('button', { name: '3M' })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(within(group).getByRole('button', { name: '1M' }))
    expect(within(group).getByRole('button', { name: '1M' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(group).getByRole('button', { name: '3M' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('follows the poll rather than sitting still under a moving row', () => {
    render(<App />)
    openTicket()

    const price = () => screen.getByText(/is (in|out of) the money against the/).textContent

    const before = price()
    act(() => {
      useUIStore.getState().pollMarkets(2_000)
    })
    expect(price()).not.toBe(before)
  })
})
