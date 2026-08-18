import { describe, it, expect } from 'vitest'
import {
  CHAIN_UNDERLYINGS,
  MIN_VOLUME_STEPS,
  filterChain,
  rankChain,
  rankStocks,
  type ChainRank,
  type StockRank,
} from './markets'
import { OPTION_CHAIN, STOCKS, type OptionContract, type StockQuote } from './mockData'

const CHAIN_RANK_LIST: ChainRank[] = ['strike', 'volume', 'gainers', 'losers', 'iv', 'openInterest']
const STOCK_RANK_LIST: StockRank[] = ['active', 'gainers', 'losers', 'new', 'marketCap']

/** Minimal rows, so an ordering test fails for the reason it names rather
 * than because a fixture moved. */
function contract(over: Partial<OptionContract>): OptionContract {
  return {
    symbol: 'AAPL',
    strike: 230,
    expiration: '2026-08-21',
    type: 'call',
    last: 5,
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
    marketCap: 100,
    listedOn: '2000-01-01',
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

describe('rankChain', () => {
  it('reads the strike ladder by symbol, expiration, strike, then calls before puts', () => {
    const rows = rankChain(
      [
        contract({ symbol: 'TSLA', expiration: '2026-08-21', strike: 230, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-09-18', strike: 225, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 230, type: 'put' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 230, type: 'call' }),
        contract({ symbol: 'AAPL', expiration: '2026-08-21', strike: 225, type: 'call' }),
      ],
      'strike',
    )

    expect(rows.map((c) => `${c.symbol} ${c.expiration} ${c.strike}${c.type[0]}`)).toEqual([
      'AAPL 2026-08-21 225c',
      'AAPL 2026-08-21 230c',
      'AAPL 2026-08-21 230p',
      'AAPL 2026-09-18 225c',
      'TSLA 2026-08-21 230c',
    ])
  })

  it('ranks volume, open interest and IV highest first', () => {
    const rows = [
      contract({ strike: 1, volume: 10, openInterest: 30, iv: 0.1 }),
      contract({ strike: 2, volume: 30, openInterest: 10, iv: 0.3 }),
      contract({ strike: 3, volume: 20, openInterest: 20, iv: 0.2 }),
    ]

    expect(rankChain(rows, 'volume').map((c) => c.volume)).toEqual([30, 20, 10])
    expect(rankChain(rows, 'openInterest').map((c) => c.openInterest)).toEqual([30, 20, 10])
    expect(rankChain(rows, 'iv').map((c) => c.iv)).toEqual([0.3, 0.2, 0.1])
  })

  it('puts the biggest gain on top of gainers and the biggest loss on top of losers', () => {
    const rows = [
      contract({ strike: 1, changePct: 4 }),
      contract({ strike: 2, changePct: -9 }),
      contract({ strike: 3, changePct: 0 }),
    ]

    // Losers is the same list read from the other end, not the reverse of
    // the gainers *page* — reversing a paginated gainers list would put
    // the flattest contracts on top of the losers screen.
    expect(rankChain(rows, 'gainers').map((c) => c.changePct)).toEqual([4, 0, -9])
    expect(rankChain(rows, 'losers').map((c) => c.changePct)).toEqual([-9, 0, 4])
  })

  it('never reorders the chain in place', () => {
    // Every view reads the same module-level array. An in-place sort would
    // leave the previous view's ordering behind in the fixture, so the
    // strike ladder would come back shuffled by whatever screen ran last.
    const before = OPTION_CHAIN.map((c) => `${c.symbol}${c.strike}${c.type}${c.expiration}`)

    for (const rank of CHAIN_RANK_LIST) rankChain(OPTION_CHAIN, rank)

    expect(OPTION_CHAIN.map((c) => `${c.symbol}${c.strike}${c.type}${c.expiration}`)).toEqual(before)
  })

  it('returns every contract it was given, whichever ranking is asked for', () => {
    for (const rank of CHAIN_RANK_LIST) {
      expect(rankChain(OPTION_CHAIN, rank)).toHaveLength(OPTION_CHAIN.length)
    }
  })
})

describe('rankStocks', () => {
  it('ranks volume and percent change the way each view claims', () => {
    const rows = [
      stock({ symbol: 'A', volume: 10, changePct: 1 }),
      stock({ symbol: 'B', volume: 30, changePct: -4 }),
      stock({ symbol: 'C', volume: 20, changePct: 7 }),
    ]

    expect(rankStocks(rows, 'active').map((s) => s.symbol)).toEqual(['B', 'C', 'A'])
    expect(rankStocks(rows, 'gainers').map((s) => s.symbol)).toEqual(['C', 'A', 'B'])
    expect(rankStocks(rows, 'losers').map((s) => s.symbol)).toEqual(['B', 'A', 'C'])
  })

  it('puts the most recent listing first', () => {
    const rows = [
      stock({ symbol: 'OLD', listedOn: '1980-12-12' }),
      stock({ symbol: 'NEW', listedOn: '2025-06-05' }),
      stock({ symbol: 'MID', listedOn: '2014-10-31' }),
    ]

    expect(rankStocks(rows, 'new').map((s) => s.symbol)).toEqual(['NEW', 'MID', 'OLD'])
  })

  it('sorts a fund last rather than treating its null market cap as zero', () => {
    const rows = [
      stock({ symbol: 'SPY', marketCap: null }),
      stock({ symbol: 'SMALL', marketCap: 12 }),
      stock({ symbol: 'BIG', marketCap: 3_540 }),
    ]

    // The bug this pins: coercing null to 0 ranks SPY below the smallest
    // company on the list and asserts, in a column of dollars, that an ETF
    // is worth nothing.
    expect(rankStocks(rows, 'marketCap').map((s) => s.symbol)).toEqual(['BIG', 'SMALL', 'SPY'])
  })

  it('orders funds against each other deterministically', () => {
    const rows = [
      stock({ symbol: 'QQQ', marketCap: null }),
      stock({ symbol: 'ARKK', marketCap: null }),
      stock({ symbol: 'SPY', marketCap: null }),
    ]

    expect(rankStocks(rows, 'marketCap').map((s) => s.symbol)).toEqual(['ARKK', 'QQQ', 'SPY'])
  })

  it('never reorders the universe in place, and never drops a symbol', () => {
    const before = STOCKS.map((s) => s.symbol)

    for (const rank of STOCK_RANK_LIST) {
      expect(rankStocks(STOCKS, rank)).toHaveLength(STOCKS.length)
    }

    expect(STOCKS.map((s) => s.symbol)).toEqual(before)
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
