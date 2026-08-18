import { describe, it, expect } from 'vitest'
import {
  CHAIN_LADDER_SORT,
  CHAIN_RANKS,
  CHAIN_RANK_SORT,
  CHAIN_UNDERLYINGS,
  MIN_VOLUME_STEPS,
  STOCK_RANKS,
  STOCK_RANK_SORT,
  chainRankFor,
  filterChain,
  liveStocks,
  relativeVolume,
  searchUnderlyings,
  sortChain,
  sortStocks,
  stockRankFor,
  type ChainSortKey,
  type StockSortKey,
} from './markets'
import {
  OPTION_CHAIN,
  STOCKS,
  type OptionContract,
  type StockQuote,
  type UnderlyingQuote,
} from './mockData'

const CHAIN_KEYS: ChainSortKey[] = [
  'ladder',
  'last',
  'change',
  'changePct',
  'bid',
  'ask',
  'volume',
  'openInterest',
  'iv',
]
const STOCK_KEYS: StockSortKey[] = ['price', 'change', 'changePct', 'volume', 'relVolume', 'marketCap']

/** Minimal rows, so an ordering test fails for the reason it names rather
 * than because a fixture moved. */
function contract(over: Partial<OptionContract>): OptionContract {
  return {
    symbol: 'AAPL',
    strike: 230,
    expiration: '2026-08-21',
    type: 'call',
    last: 5,
    previousClose: 4.9,
    change: 0.1,
    changePct: 2,
    bid: 4.95,
    ask: 5.05,
    volume: 1_000,
    openInterest: 5_000,
    iv: 0.3,
    ...over,
  }
}

function stock(over: Partial<StockQuote>): StockQuote {
  return {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    price: 100,
    change: 1,
    changePct: 1,
    volume: 1_000_000,
    avgVolume: 1_000_000,
    marketCap: 100,
    ...over,
  }
}

describe('filterChain', () => {
  it('narrows to one underlying, and to the whole board on null', () => {
    const rows = [contract({ symbol: 'AAPL' }), contract({ symbol: 'TSLA' })]

    expect(filterChain(rows, { underlying: 'TSLA', minVolume: 0 }).map((c) => c.symbol)).toEqual([
      'TSLA',
    ])
    expect(filterChain(rows, { underlying: null, minVolume: 0 })).toHaveLength(2)
  })

  it('keeps contracts at the volume floor, not just above it', () => {
    const rows = [contract({ volume: 4_999 }), contract({ volume: 5_000 }), contract({ volume: 5_001 })]

    // A ">= 5,000" filter that drops the contract trading exactly 5,000 is
    // off by one against its own label.
    expect(filterChain(rows, { underlying: null, minVolume: 5_000 }).map((c) => c.volume)).toEqual([
      5_000, 5_001,
    ])
  })
})

describe('searchUnderlyings', () => {
  it('matches anywhere in the symbol, case insensitively', () => {
    const symbols = ['SPY', 'AAPL', 'QQQ', 'MSFT']

    expect(searchUnderlyings(symbols, 'qq')).toEqual(['QQQ'])
    expect(searchUnderlyings(symbols, 'S')).toEqual(['SPY', 'MSFT'])
    // Unanchored on purpose: nobody should have to know whether the list is
    // alphabetical to find a symbol in it.
    expect(searchUnderlyings(symbols, 'ft')).toEqual(['MSFT'])
  })

  it('returns everything for an empty query and nothing for a miss', () => {
    const symbols = ['SPY', 'AAPL']

    expect(searchUnderlyings(symbols, '   ')).toEqual(symbols)
    // "APPL" is the classic typo. Finding nothing is the correct answer —
    // silently matching AAPL would filter a chain to a symbol nobody asked
    // for.
    expect(searchUnderlyings(symbols, 'APPL')).toEqual([])
  })
})

describe('sortChain', () => {
  it('reads the ladder by symbol, expiration, strike, then calls before puts', () => {
    const rows = sortChain(
      [
        contract({ symbol: 'TSLA', expiration: '2026-08-21', strike: 230, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-09-18', strike: 225, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 230, type: 'put' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 230, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 225, type: 'call' }),
      ],
      CHAIN_LADDER_SORT,
    )

    expect(rows.map((c) => `${c.symbol} ${c.expiration} ${c.strike}${c.type[0]}`)).toEqual([
      'AAPL 2026-08-21 225c',
      'AAPL 2026-08-21 230c',
      'AAPL 2026-08-21 230p',
      'AAPL 2026-09-18 225c',
      'TSLA 2026-08-21 230c',
    ])
  })

  it('sorts every quote column in both directions', () => {
    const rows = [
      contract({ strike: 1, volume: 10, openInterest: 30, iv: 0.1, last: 3, changePct: 4, bid: 1, ask: 9 }),
      contract({ strike: 2, volume: 30, openInterest: 10, iv: 0.3, last: 1, changePct: -9, bid: 3, ask: 7 }),
      contract({ strike: 3, volume: 20, openInterest: 20, iv: 0.2, last: 2, changePct: 0, bid: 2, ask: 8 }),
    ]

    for (const key of CHAIN_KEYS) {
      if (key === 'ladder') continue
      const down = sortChain(rows, { key, direction: 'descending' }).map((c) => c[key])
      const up = sortChain(rows, { key, direction: 'ascending' }).map((c) => c[key])

      expect(down).toEqual([...down].sort((a, b) => b - a))
      expect(up).toEqual([...up].sort((a, b) => a - b))
    }
  })

  it('breaks ties on the ladder rather than on array order', () => {
    // Two contracts on the same volume must not swap places between
    // renders — a table that reshuffles under a stable sort looks like it
    // is still loading.
    const rows = [
      contract({ strike: 240, volume: 500 }),
      contract({ strike: 220, volume: 500 }),
      contract({ strike: 230, volume: 500 }),
    ]

    expect(sortChain(rows, { key: 'volume', direction: 'descending' }).map((c) => c.strike)).toEqual([
      220, 230, 240,
    ])
  })

  it('never reorders the chain in place', () => {
    // Every view reads the same array. An in-place sort would leave the
    // previous view's ordering behind in the data, so the ladder would come
    // back shuffled by whatever screen ran last.
    const before = OPTION_CHAIN.map((c) => `${c.symbol}${c.strike}${c.type}${c.expiration}`)

    for (const key of CHAIN_KEYS) {
      for (const direction of ['ascending', 'descending'] as const) {
        expect(sortChain(OPTION_CHAIN, { key, direction })).toHaveLength(OPTION_CHAIN.length)
      }
    }

    expect(OPTION_CHAIN.map((c) => `${c.symbol}${c.strike}${c.type}${c.expiration}`)).toEqual(before)
  })
})

describe('the named screens and the column headers share one sort', () => {
  it('round-trips every chain preset', () => {
    for (const rank of CHAIN_RANKS) {
      expect(chainRankFor(CHAIN_RANK_SORT[rank])).toBe(rank)
    }
  })

  it('round-trips every stock preset', () => {
    for (const rank of STOCK_RANKS) {
      expect(stockRankFor(STOCK_RANK_SORT[rank])).toBe(rank)
    }
  })

  it('reports no preset once a header has taken the table somewhere custom', () => {
    // The dropdown shows "Custom" rather than keeping a stale label
    // claiming the table is still ranked by IV.
    expect(chainRankFor({ key: 'bid', direction: 'ascending' })).toBeNull()
    expect(stockRankFor({ key: 'price', direction: 'ascending' })).toBeNull()
  })

  it('keeps losers as its own ascending sort, not the gainers list reversed', () => {
    expect(CHAIN_RANK_SORT.losers).toEqual({ key: 'changePct', direction: 'ascending' })
    expect(CHAIN_RANK_SORT.gainers).toEqual({ key: 'changePct', direction: 'descending' })
    expect(STOCK_RANK_SORT.losers).toEqual({ key: 'changePct', direction: 'ascending' })
  })

  it('points trending at relative volume and most active at raw volume', () => {
    // Two different questions. Raw volume finds the same mega caps every
    // session; relative volume finds the name having an unusual day.
    expect(STOCK_RANK_SORT.trending).toEqual({ key: 'relVolume', direction: 'descending' })
    expect(STOCK_RANK_SORT.active).toEqual({ key: 'volume', direction: 'descending' })
  })
})

describe('sortStocks', () => {
  it('sorts every column in both directions', () => {
    const rows = [
      stock({ symbol: 'A', price: 10, change: 1, changePct: 1, volume: 30, avgVolume: 10, marketCap: 5 }),
      stock({ symbol: 'B', price: 30, change: -3, changePct: -3, volume: 10, avgVolume: 20, marketCap: 50 }),
      stock({ symbol: 'C', price: 20, change: 2, changePct: 7, volume: 20, avgVolume: 40, marketCap: 15 }),
    ]

    for (const key of STOCK_KEYS) {
      const read = (s: StockQuote) => (key === 'relVolume' ? relativeVolume(s) : (s[key] as number))
      expect(sortStocks(rows, { key, direction: 'descending' }).map(read)).toEqual(
        rows.map(read).sort((a, b) => b - a),
      )
      expect(sortStocks(rows, { key, direction: 'ascending' }).map(read)).toEqual(
        rows.map(read).sort((a, b) => a - b),
      )
    }
  })

  it('ranks trending by relative volume, not by size', () => {
    const rows = [
      // Enormous in absolute terms, ordinary for itself.
      stock({ symbol: 'NVDA', volume: 200_000_000, avgVolume: 210_000_000 }),
      // Small, and having a day.
      stock({ symbol: 'RBRK', volume: 12_000_000, avgVolume: 3_100_000 }),
    ]

    expect(sortStocks(rows, STOCK_RANK_SORT.trending).map((s) => s.symbol)).toEqual(['RBRK', 'NVDA'])
    expect(sortStocks(rows, STOCK_RANK_SORT.active).map((s) => s.symbol)).toEqual(['NVDA', 'RBRK'])
  })

  it('sorts a fund last in BOTH directions rather than treating null as zero', () => {
    const rows = [
      stock({ symbol: 'SPY', marketCap: null }),
      stock({ symbol: 'SMALL', marketCap: 12 }),
      stock({ symbol: 'BIG', marketCap: 3_540 }),
    ]

    // Coerced to zero, SPY ranks below the smallest company on the list and
    // a column of dollars states that a fund is worth nothing. Ascending is
    // the case that catches the coercion: a zero would sort *first* there.
    expect(sortStocks(rows, { key: 'marketCap', direction: 'descending' }).map((s) => s.symbol)).toEqual([
      'BIG',
      'SMALL',
      'SPY',
    ])
    expect(sortStocks(rows, { key: 'marketCap', direction: 'ascending' }).map((s) => s.symbol)).toEqual([
      'SMALL',
      'BIG',
      'SPY',
    ])
  })

  it('orders funds against each other deterministically', () => {
    const rows = [
      stock({ symbol: 'QQQ', marketCap: null }),
      stock({ symbol: 'ARKK', marketCap: null }),
      stock({ symbol: 'SPY', marketCap: null }),
    ]

    expect(sortStocks(rows, { key: 'marketCap', direction: 'descending' }).map((s) => s.symbol)).toEqual([
      'ARKK',
      'QQQ',
      'SPY',
    ])
  })

  it('never reorders the universe in place, and never drops a symbol', () => {
    const before = STOCKS.map((s) => s.symbol)

    for (const key of STOCK_KEYS) {
      expect(sortStocks(STOCKS, { key, direction: 'descending' })).toHaveLength(STOCKS.length)
    }

    expect(STOCKS.map((s) => s.symbol)).toEqual(before)
  })
})

describe('liveStocks', () => {
  function quote(price: number): UnderlyingQuote {
    return {
      symbol: 'AAPL',
      price,
      previousClose: price - 1,
      change: 1,
      changePct: 0.5,
      history: [{ date: '2026-08-07', value: price }],
    }
  }

  it('re-quotes a row from the live price map', () => {
    const [row] = liveStocks([stock({ symbol: 'AAPL', price: 100 })], { AAPL: quote(240) })

    // Price belongs to the symbol, not to the row. A stock carrying its own
    // copy is how the Markets table and an Activity row end up disagreeing
    // about what AAPL costs.
    expect(row.price).toBe(240)
    expect(row.change).toBe(1)
    expect(row.changePct).toBe(0.5)
  })

  it('leaves a row alone when the map has no quote for it', () => {
    const [row] = liveStocks([stock({ symbol: 'XYZ', price: 42 })], { AAPL: quote(240) })
    expect(row.price).toBe(42)
  })

  it('keeps everything that is not a quote', () => {
    const [row] = liveStocks([stock({ symbol: 'AAPL', marketCap: 3540, avgVolume: 52_000_000 })], {
      AAPL: quote(240),
    })
    expect(row.marketCap).toBe(3540)
    expect(row.avgVolume).toBe(52_000_000)
  })
})

describe('the controls the page offers are backed by the fixture', () => {
  it('lists every underlying that actually has a chain', () => {
    expect(CHAIN_UNDERLYINGS.length).toBeGreaterThan(1)
    for (const symbol of CHAIN_UNDERLYINGS) {
      expect(OPTION_CHAIN.some((c) => c.symbol === symbol)).toBe(true)
    }
    // Derived from the chain rather than kept as a second list, so the two
    // cannot drift.
    expect(new Set(OPTION_CHAIN.map((c) => c.symbol)).size).toBe(CHAIN_UNDERLYINGS.length)
  })

  it('gives every volume step something to do', () => {
    // A threshold that filters nothing is a control that lies about being
    // one. Each step has to cut the previous result set.
    let previous = OPTION_CHAIN.length
    for (const step of MIN_VOLUME_STEPS.slice(1)) {
      const kept = filterChain(OPTION_CHAIN, { underlying: null, minVolume: step }).length
      expect(kept).toBeLessThan(previous)
      expect(kept).toBeGreaterThan(0)
      previous = kept
    }
  })

  it('leaves the empty state reachable from the controls alone', () => {
    // CLAUDE.md: a state absent from the fixtures is one nobody can see.
    // The thin name at the top volume floor is where that message lives.
    const highest = MIN_VOLUME_STEPS[MIN_VOLUME_STEPS.length - 1]
    const emptySomewhere = CHAIN_UNDERLYINGS.some(
      (symbol) => filterChain(OPTION_CHAIN, { underlying: symbol, minVolume: highest }).length === 0,
    )

    expect(emptySomewhere).toBe(true)
  })
})
