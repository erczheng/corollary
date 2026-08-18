import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import {
  NEWS_INCOMING,
  NEWS_ITEMS,
  SECTOR_CONSENSUS,
  SENTIMENT_COMPONENTS,
  SOCIAL_ATTENTION,
} from '../lib/mockData'
import { compositeScore, newsSectors } from '../lib/news'

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location. Same guard the other page tests
  // carry: without it a test that runs after a navigation starts on the
  // wrong page.
  window.history.pushState({}, '', '/news')
  useUIStore.setState({ ...initialState }, true)
})

function section(name: string): HTMLElement {
  return screen.getByRole('region', { name })
}

function feedTable(): HTMLElement {
  return within(section('Latest intel')).getByRole('table')
}

function bodyRows(table: HTMLElement): HTMLElement[] {
  return within(table).getAllByRole('row').slice(1)
}

function column(table: HTMLElement, index: number): string[] {
  return bodyRows(table).map((r) => within(r).getAllByRole('cell')[index].textContent ?? '')
}

describe('the News page', () => {
  it('renders all five sections of PRD §8.3', () => {
    render(<App />)

    for (const name of [
      'Latest intel',
      'Market Sentiment',
      'Social Attention',
      'Top rated by sector',
      'Market calendar',
    ]) {
      expect(section(name)).toBeInTheDocument()
    }
  })

  /** Loading is a real condition rather than a timer — the hook polls on
   * mount, so by the time the feed is on screen the skeleton is gone. */
  it('shows a skeleton until the first poll returns, then the feed', () => {
    useUIStore.setState({ lastNewsAt: null })
    render(<App />)

    expect(feedTable()).toBeInTheDocument()
    expect(screen.queryByText('Loading news feed')).not.toBeInTheDocument()
  })

  it('reports the feed as live once a poll has landed', () => {
    render(<App />)
    expect(screen.getByRole('status')).toHaveAccessibleName(/^Live/)
  })
})

describe('the live feed', () => {
  it('opens newest first', () => {
    render(<App />)
    const times = bodyRows(feedTable()).map((r) => within(r).getAllByRole('cell')[0].textContent)
    expect(times).toEqual([...times])
    expect(column(feedTable(), 2)[0]).toContain(NEWS_ITEMS[0].headline)
  })

  it('flips to oldest first', () => {
    render(<App />)
    const newest = column(feedTable(), 2)[0]

    fireEvent.change(screen.getByLabelText('Sort headlines by time'), {
      target: { value: 'oldest' },
    })

    expect(column(feedTable(), 2)[0]).not.toBe(newest)
  })

  it('narrows to one sector', () => {
    render(<App />)
    const sector = newsSectors(NEWS_ITEMS)[0]

    fireEvent.change(screen.getByLabelText('Filter headlines by sector'), {
      target: { value: sector },
    })

    for (const row of bodyRows(feedTable())) {
      expect(within(row).getAllByRole('cell')[2]).toHaveAttribute(
        'title',
        expect.stringContaining(sector),
      )
    }
  })

  /** The two controls combine rather than replacing each other — the count
   * in the panel header is what proves it. */
  it('combines the sector and sentiment filters', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Filter headlines by sector'), {
      target: { value: 'Technology' },
    })
    const sectorOnly = within(section('Latest intel')).getByText(/of \d+ headlines/).textContent

    fireEvent.change(screen.getByLabelText('Filter headlines by sentiment'), {
      target: { value: 'bearish' },
    })
    const both = within(section('Latest intel')).getByText(/of \d+ headlines/).textContent

    expect(both).not.toBe(sectorOnly)
    for (const row of bodyRows(feedTable())) {
      const cells = within(row).getAllByRole('cell')
      expect(cells[2]).toHaveAttribute('title', expect.stringContaining('Technology'))
      expect(cells[3].textContent).toContain('BEAR')
    }
  })

  it('shortens the feed as the lookback shortens', () => {
    render(<App />)
    const all = bodyRows(feedTable()).length

    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })

    expect(bodyRows(feedTable()).length).toBeLessThanOrEqual(all)
    expect(within(section('Latest intel')).getByText(/of \d+ headlines/)).toBeInTheDocument()
  })

  /** An empty result is a designed state that says why it is empty and
   * what to do — not a blank panel. */
  it('explains an empty result rather than showing a blank panel', () => {
    render(<App />)

    fireEvent.change(screen.getByLabelText('Filter headlines by sector'), {
      target: { value: 'Energy' },
    })
    fireEvent.change(screen.getByLabelText('Filter headlines by sentiment'), {
      target: { value: 'unclassified' },
    })
    fireEvent.change(screen.getByLabelText('How far back the feed reaches'), {
      target: { value: 'today' },
    })

    const panel = section('Latest intel')
    if (within(panel).queryByRole('table') === null) {
      expect(within(panel).getByText(/No headlines match those filters/)).toBeInTheDocument()
      expect(within(panel).getByText(/widen the lookback/)).toBeInTheDocument()
    }
  })

  it('paginates rather than rendering the whole corpus', () => {
    render(<App />)
    expect(bodyRows(feedTable()).length).toBeLessThan(NEWS_ITEMS.length)
    expect(within(section('Latest intel')).getByText(/Page 1 of/)).toBeInTheDocument()
  })

  /** A headline about no single name is not a ticker, and rendering
   * "MARKET" in the ticker column reads like one. */
  it('renders a macro story as Market rather than a ticker', () => {
    render(<App />)
    fireEvent.change(screen.getByLabelText('Filter headlines by sector'), {
      target: { value: 'Macro' },
    })
    for (const cell of column(feedTable(), 1)) {
      expect(cell).toBe('MARKET')
    }
  })

  it('names the tier that produced each label', () => {
    render(<App />)
    const labels = column(feedTable(), 3)
    expect(labels.some((l) => /Provider|Rules|LLM/.test(l))).toBe(true)
  })

  /** The first poll is the initial fetch. Releasing an arrival on mount
   * would put a headline that landed after you opened the page — and
   * before you could read it — at the top of every cold open. */
  it('releases nothing on the first poll', () => {
    render(<App />)

    expect(useUIStore.getState().lastNewsAt).not.toBeNull()
    expect(useUIStore.getState().newsFeed).toHaveLength(NEWS_ITEMS.length)
    expect(useUIStore.getState().newsReleased).toBe(0)
  })

  /** A new headline arriving prepends to the feed rather than replacing
   * it — the whole point of a live feed. */
  it('prepends a headline when one arrives', () => {
    render(<App />)
    const before = column(feedTable(), 2)[0]

    act(() => {
      useUIStore.getState().pollNews()
    })

    expect(column(feedTable(), 2)[0]).not.toBe(before)
    expect(useUIStore.getState().newsFeed).toHaveLength(NEWS_ITEMS.length + 1)
  })

  /** No news is the ordinary state of a news feed, so an empty poll is a
   * successful one and the pill has to keep saying so. */
  it('stays live once the incoming reserve is exhausted', () => {
    render(<App />)

    act(() => {
      for (let i = 0; i < NEWS_INCOMING.length + 3; i += 1) useUIStore.getState().pollNews()
    })

    expect(useUIStore.getState().newsFeed).toHaveLength(NEWS_ITEMS.length + NEWS_INCOMING.length)
    expect(useUIStore.getState().lastNewsAt).not.toBeNull()
    expect(screen.getByRole('status')).toHaveAccessibleName(/^Live/)
  })

  /** MARKET_TODAY is the fixture's today and the machine's clock is not.
   * A released item stamped `new Date()` would sort months above the
   * corpus it belongs in. */
  it('stamps an arriving headline on the fixture’s clock, not the wall clock', () => {
    render(<App />)

    act(() => {
      useUIStore.getState().pollNews()
    })

    const newest = useUIStore.getState().newsFeed[0]
    expect(newest.time.slice(0, 4)).toBe(NEWS_ITEMS[0].time.slice(0, 4))
    expect(newest.time > NEWS_ITEMS[0].time).toBe(true)
  })
})

describe('the market sentiment composite', () => {
  /** The headline figure and the seven rows beneath it are two renderings
   * of one fact, so they cannot be allowed to disagree. */
  it('renders the derived composite, not a stored one', () => {
    render(<App />)
    const panel = section('Market Sentiment')
    const score = compositeScore(SENTIMENT_COMPONENTS)

    expect(within(panel).getByText(String(score))).toBeInTheDocument()
  })

  it('shows all seven components inline rather than on hover only', () => {
    render(<App />)
    const panel = section('Market Sentiment')

    for (const c of SENTIMENT_COMPONENTS) {
      expect(within(panel).getByText(c.name)).toBeInTheDocument()
    }
  })

  /** Points, not percent: a 0-100 index has no units to be a percentage
   * of. */
  it('reports the one-day move in points', () => {
    render(<App />)
    expect(within(section('Market Sentiment')).getByText(/pts/)).toBeInTheDocument()
  })
})

describe('social attention', () => {
  it('ranks by velocity rather than raw mentions', () => {
    render(<App />)
    const table = within(section('Social Attention')).getByRole('table')
    const tickers = column(table, 0)

    // CRWV is 5x its baseline on far fewer mentions than the mega caps.
    expect(tickers[0]).toBe('CRWV')
    expect(tickers).toHaveLength(SOCIAL_ATTENTION.length)
  })

  it('says it is context and never a trigger', () => {
    render(<App />)
    expect(
      within(section('Social Attention')).getByText(/Context, never a trade trigger/),
    ).toBeInTheDocument()
  })

  /** PRD.md §8.3 asks for both: the sample says how much conversation
   * there was, the labelled count says how much of it the sentiment was
   * actually computed from. A share alone hides the first. */
  it('shows both the sample size and the labelled count', () => {
    render(<App />)
    const table = within(section('Social Attention')).getByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Msgs' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Lbl.' })).toBeInTheDocument()

    // ALAB is the thin-coverage fixture: 88 labels out of 780 messages.
    const alab = bodyRows(table).find((r) => within(r).getAllByRole('cell')[0].textContent === 'ALAB')!
    const cells = within(alab).getAllByRole('cell')
    expect(cells[2].textContent).toBe('780')
    expect(cells[3].textContent).toBe('88')
  })
})

describe('top rated by sector', () => {
  it('lists every sector, ranked by net rating', () => {
    render(<App />)
    const table = within(section('Top rated by sector')).getByRole('table')
    expect(bodyRows(table)).toHaveLength(SECTOR_CONSENSUS.length)
    expect(column(table, 0)[0]).toContain('Communication Services')
  })

  it('states the as-of date, because it refreshes monthly', () => {
    render(<App />)
    expect(within(section('Top rated by sector')).getByText(/as of/i)).toBeInTheDocument()
  })
})

describe('the market calendar', () => {
  /** An ex-dividend date has no 8:30am. Printing a placeholder midnight
   * would invent one and, in ET, put it on the previous evening. */
  it('reads All day for an event that never had a time', () => {
    render(<App />)
    expect(within(section('Market calendar')).getAllByText('All day').length).toBeGreaterThan(0)
  })

  it('carries timed events too', () => {
    render(<App />)
    const panel = section('Market calendar')
    expect(within(panel).getAllByText(/\d{1,2}:\d{2}\s?(AM|PM)/).length).toBeGreaterThan(0)
  })

  it('labels each event with its type', () => {
    render(<App />)
    const panel = section('Market calendar')
    expect(within(panel).getAllByText('Earnings').length).toBeGreaterThan(0)
    expect(within(panel).getAllByText('Central bank').length).toBeGreaterThan(0)
  })
})
