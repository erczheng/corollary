import { describe, it, expect } from 'vitest'
import {
  CHAIN_LADDER_SORT,
  CHAIN_RANKS,
  CHAIN_RANK_SORT,
  MARKETS_VIEWPORT_DEBOUNCE_MS,
  MAX_MARKETS_VISIBLE_SYMBOLS,
  MIN_VOLUME_STEPS,
  STOCK_RANKS,
  STOCK_RANK_SORT,
  chainRankFor,
  contractMoneyness,
  filterChain,
  ivSourceOf,
  latestVolumeDate,
  marketsVisibleDiffers,
  marketsVisibleHint,
  relativeVolume,
  searchStocks,
  searchUnderlyings,
  sortChain,
  sortStocks,
  stockRankFor,
  underlyingSymbols,
  volumeBasis,
  type ChainSortKey,
  type SortableStock,
  type StockSortKey,
} from './markets'
import { OPTION_CHAIN, STOCKS } from './mockData'
import { changeOf, changePctOf } from './quotes'
import { type OptionContract, type StockQuote } from './types'

const CHAIN_KEYS: ChainSortKey[] = [
  'ladder',
  'last',
  'change',
  'changePct',
  'bid',
  'ask',
  'volume',
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

/** A **derived** row, not a wire row: `change` and `changePct` are not on
 * `StockQuote` any more — the wire carries `previousClose` and `quotes.ts`
 * derives the pair at read time — and `sortStocks` sorts the derived shape
 * because that is the shape the table renders. */
function stock(over: Partial<SortableStock>): SortableStock {
  return {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    price: 100,
    at: '2026-08-07T19:45:00Z',
    previousClose: 99,
    change: 1,
    changePct: 1,
    volume: 1_000_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-08-07',
    avgVolume: 1_000_000,
    marketCap: 100,
    ...over,
  }
}

describe('filterChain', () => {
  it('narrows on volume and leaves the underlying to the fetch', () => {
    // Phase 1 filtered the underlying here too, because one fixture array
    // held every chain and `symbol` was the underlying. Live, the chain is
    // fetched one underlying at a time and `symbol` is the OCC contract
    // symbol — `NVDA260914C00210000` — so filtering on it again would
    // compare that against `NVDA` and empty the table.
    const rows = [
      contract({ symbol: 'NVDA260914C00210000', volume: 100 }),
      contract({ symbol: 'NVDA260914P00220000', volume: 9_000 }),
    ]

    expect(filterChain(rows, { minVolume: 0 })).toHaveLength(2)
    expect(filterChain(rows, { minVolume: 5_000 }).map((c) => c.symbol)).toEqual([
      'NVDA260914P00220000',
    ])
  })

  it('keeps contracts at the volume floor, not just above it', () => {
    const rows = [contract({ volume: 4_999 }), contract({ volume: 5_000 }), contract({ volume: 5_001 })]

    // A ">= 5,000" filter that drops the contract trading exactly 5,000 is
    // off by one against its own label.
    expect(filterChain(rows, { minVolume: 5_000 }).map((c) => c.volume)).toEqual([
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
  it('reads the ladder by expiration, strike, then calls before puts', () => {
    // One underlying, because that is how a chain is fetched now. `symbol`
    // is the OCC contract symbol and encodes the right *before* the strike,
    // so sorting on it first would group every call above every put and
    // stop the ladder reading as a ladder.
    const rows = sortChain(
      [
        contract({ symbol: 'AAPL260918C00225000', expiration: '2026-09-18', strike: 225, type: 'call' }),
        contract({ symbol: 'AAPL260821P00230000', expiration: '2026-08-21', strike: 230, type: 'put' }),
        contract({ symbol: 'AAPL260821C00230000', expiration: '2026-08-21', strike: 230, type: 'call' }),
        contract({ symbol: 'AAPL260821C00225000', expiration: '2026-08-21', strike: 225, type: 'call' }),
      ],
      CHAIN_LADDER_SORT,
    )

    expect(rows.map((c) => `${c.expiration} ${c.strike}${c.type[0]}`)).toEqual([
      '2026-08-21 225c',
      '2026-08-21 230c',
      '2026-08-21 230p',
      '2026-09-18 225c',
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
      // Every figure on these rows is present, so `as number` reads the
      // fixture rather than weakening the assertion. Null ordering has its
      // own test below.
      const down = sortChain(rows, { key, direction: 'descending' }).map((c) => c[key] as number)
      const up = sortChain(rows, { key, direction: 'ascending' }).map((c) => c[key] as number)

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
      const read = (s: SortableStock) =>
        (key === 'relVolume' ? relativeVolume(s) : s[key]) as number
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
    // The fixtures are wire rows; the table sorts the derived rows, so the
    // day is derived here exactly as `liveStockRows` derives it.
    const derived = STOCKS.map((s) => ({
      ...s,
      change: changeOf(s),
      changePct: changePctOf(s),
    }))

    for (const key of STOCK_KEYS) {
      expect(sortStocks(derived, { key, direction: 'descending' })).toHaveLength(STOCKS.length)
    }

    expect(STOCKS.map((s) => s.symbol)).toEqual(before)
  })
})

describe('an absent figure is not a small one', () => {
  it('sorts a null chain column last in both directions', () => {
    // The rule the fund market cap has always followed, now shared with
    // every column a real feed leaves empty: half a live ladder has no bid
    // at all, and a bid of null is not a bid of zero.
    const rows = [
      contract({ strike: 1, bid: 2 }),
      contract({ strike: 2, bid: null }),
      contract({ strike: 3, bid: 9 }),
    ]

    expect(sortChain(rows, { key: 'bid', direction: 'descending' }).map((c) => c.bid)).toEqual([
      9, 2, null,
    ])
    expect(sortChain(rows, { key: 'bid', direction: 'ascending' }).map((c) => c.bid)).toEqual([
      2, 9, null,
    ])
  })

  it('sorts a null stock column last in both directions', () => {
    const rows = [
      stock({ symbol: 'A', volume: 10 }),
      stock({ symbol: 'B', volume: null }),
      stock({ symbol: 'C', volume: 30 }),
    ]

    expect(
      sortStocks(rows, { key: 'volume', direction: 'descending' }).map((s) => s.volume),
    ).toEqual([30, 10, null])
    expect(sortStocks(rows, { key: 'volume', direction: 'ascending' }).map((s) => s.volume)).toEqual(
      [10, 30, null],
    )
  })

  it('has no relative volume without both of its sides', () => {
    // The denominator is the load-bearing one: a 0 average is a division by
    // zero, so a name whose history could not be read would sort *first* on
    // the screen built to find unusual activity.
    expect(relativeVolume(stock({ volume: 2_000, avgVolume: 1_000 }))).toBe(2)
    expect(relativeVolume(stock({ volume: null, avgVolume: 1_000 }))).toBeNull()
    expect(relativeVolume(stock({ volume: 2_000, avgVolume: null }))).toBeNull()
    expect(relativeVolume(stock({ volume: 2_000, avgVolume: 0 }))).toBeNull()
  })

  it('keeps a contract with no volume out of every floor above zero', () => {
    const rows = [contract({ strike: 1, volume: null }), contract({ strike: 2, volume: 9_000 })]

    expect(filterChain(rows, { minVolume: 0 })).toHaveLength(2)
    expect(filterChain(rows, { minVolume: 5_000 }).map((c) => c.strike)).toEqual([2])
  })

  it('reads the IV source off the wire without declaring it', () => {
    // `iv_source` is served and `types.ts` deliberately does not declare it,
    // so a chain can still say which numbers it solved for itself.
    expect(ivSourceOf(contract({}))).toBeNull()
    expect(ivSourceOf({ ...contract({}), ivSource: 'derived' } as OptionContract)).toBe('derived')
    expect(ivSourceOf({ ...contract({}), ivSource: 'vendor' } as OptionContract)).toBe('vendor')
  })
})

describe('which session the volume column is counting', () => {
  const latest = '2026-09-11'

  it('names the running session and the completed one differently', () => {
    // A column that silently means either "half a Tuesday morning" or "all
    // of Monday" makes a busy stock read as quiet.
    expect(
      volumeBasis(stock({ volume: 1, volumeSession: 'in_progress', volumeDate: latest }), latest),
    ).toBe('partial')
    expect(
      volumeBasis(stock({ volume: 1, volumeSession: 'completed', volumeDate: latest }), latest),
    ).toBe('session')
  })

  it('calls a completed session stale once a later one exists', () => {
    expect(
      volumeBasis(
        stock({ volume: 1, volumeSession: 'completed', volumeDate: '2026-09-04' }),
        latest,
      ),
    ).toBe('stale')
  })

  it('reports an absent volume as absent rather than as a session', () => {
    expect(volumeBasis(stock({ volume: null, volumeSession: null, volumeDate: null }), latest)).toBe(
      'absent',
    )
  })

  it('reports a session the server never sent as absent, not as complete', () => {
    // Not hypothetical, and worse than the crash it replaced. A server that
    // predates the volume-session commit omits these keys rather than nulling
    // them, and `request<StockQuote[]>` casts unvalidated JSON, so `undefined`
    // reaches here under a type that promises `string | null`. Every guard was
    // strict, `undefined === null` is false, and the fall-through was
    // `'session'` -- so a half-traded morning rendered "full session" and a
    // mega cap at a quarter of its usual volume read as dead quiet.
    const absent = stock({ volume: 1, volumeSession: 'completed', volumeDate: latest }) as unknown as Record<
      string,
      unknown
    >
    delete absent.volumeSession
    delete absent.volumeDate

    expect(volumeBasis(absent as unknown as StockQuote, latest)).toBe('absent')
  })

  it('never returns a date the response did not carry', () => {
    // `undefined !== null` is true, so the strict guard assigned `undefined`
    // to the accumulator and returned it under a `string | null` annotation.
    const row = stock({ volume: 1, volumeSession: 'completed', volumeDate: latest }) as unknown as Record<
      string,
      unknown
    >
    delete row.volumeDate

    expect(latestVolumeDate([row as unknown as StockQuote])).toBeNull()
  })

  it('takes the latest trading day from the response, never from the clock', () => {
    // Every boundary here is a New York one and the browser's clock is not.
    expect(
      latestVolumeDate([
        stock({ symbol: 'A', volumeDate: '2026-09-04' }),
        stock({ symbol: 'B', volumeDate: latest }),
        stock({ symbol: 'C', volumeDate: null }),
      ]),
    ).toBe(latest)
    expect(latestVolumeDate([stock({ symbol: 'A', volumeDate: null })])).toBeNull()
    expect(latestVolumeDate([])).toBeNull()
  })
})

describe('the controls the page offers are backed by the fixture', () => {
  it('offers every quoted symbol as an underlying, once and in order', () => {
    // Derived from the served universe rather than kept as a second list,
    // so the combobox and the table cannot drift apart.
    const symbols = underlyingSymbols(STOCKS)

    expect(symbols.length).toBeGreaterThan(1)
    expect(new Set(symbols).size).toBe(symbols.length)
    expect(symbols).toEqual([...symbols].sort((a, b) => a.localeCompare(b)))
    for (const row of STOCKS) expect(symbols).toContain(row.symbol)
  })

  it('gives every volume step something to do', () => {
    // A threshold that filters nothing is a control that lies about being
    // one. Each step has to cut the previous result set.
    let previous = OPTION_CHAIN.length
    for (const step of MIN_VOLUME_STEPS.slice(1)) {
      const kept = filterChain(OPTION_CHAIN, { minVolume: step }).length
      expect(kept).toBeLessThan(previous)
      expect(kept).toBeGreaterThan(0)
      previous = kept
    }
  })

  it('leaves the empty state reachable from the controls alone', () => {
    // CLAUDE.md: a state absent from the fixtures is one nobody can see.
    // The thin name at the top volume floor is where that message lives.
    const highest = MIN_VOLUME_STEPS[MIN_VOLUME_STEPS.length - 1]
    // One underlying at a time, the way the page fetches it.
    const emptySomewhere = [...new Set(OPTION_CHAIN.map((c) => c.symbol))].some(
      (symbol) =>
        filterChain(
          OPTION_CHAIN.filter((c) => c.symbol === symbol),
          { minVolume: highest },
        ).length === 0,
    )

    expect(emptySomewhere).toBe(true)
  })
})

describe('searchStocks', () => {
  const universe = [
    stock({ symbol: 'RDDT', name: 'Reddit Inc.' }),
    stock({ symbol: 'NVDA', name: 'NVIDIA Corp.' }),
    stock({ symbol: 'ARKK', name: 'ARK Innovation ETF' }),
  ]

  it('matches a ticker and a company name alike', () => {
    // Half the reason to search a screener is that you know the company and
    // not the ticker.
    expect(searchStocks(universe, 'nvd').map((s) => s.symbol)).toEqual(['NVDA'])
    expect(searchStocks(universe, 'reddit').map((s) => s.symbol)).toEqual(['RDDT'])
    expect(searchStocks(universe, 'ETF').map((s) => s.symbol)).toEqual(['ARKK'])
  })

  it('returns everything for an empty query and nothing for a miss', () => {
    expect(searchStocks(universe, '  ')).toHaveLength(3)
    expect(searchStocks(universe, 'zzzz')).toEqual([])
  })

  it('never reorders or mutates the universe', () => {
    const before = universe.map((s) => s.symbol)
    searchStocks(universe, '')
    expect(universe.map((s) => s.symbol)).toEqual(before)
  })
})

describe('contractMoneyness', () => {
  it('puts a call in the money above its strike and a put below it', () => {
    // Inverted, a ticket would call a put worthless at exactly the moment
    // it was worth the most.
    expect(contractMoneyness(232.4, 230, 'call').itm).toBe(true)
    expect(contractMoneyness(232.4, 235, 'call').itm).toBe(false)
    expect(contractMoneyness(232.4, 235, 'put').itm).toBe(true)
    expect(contractMoneyness(232.4, 230, 'put').itm).toBe(false)
  })

  it('reports distance unsigned, since which way it points is what itm says', () => {
    expect(contractMoneyness(232.4, 230, 'call').distance).toBeCloseTo(2.4, 2)
    expect(contractMoneyness(232.4, 235, 'call').distance).toBeCloseTo(2.6, 2)
  })

  it('treats exactly at the strike as out of the money', () => {
    // At the money is not in the money: intrinsic value is zero, and a
    // ticket claiming otherwise overstates what the contract is worth.
    expect(contractMoneyness(230, 230, 'call').itm).toBe(false)
    expect(contractMoneyness(230, 230, 'put').itm).toBe(false)
    expect(contractMoneyness(230, 230, 'call').distance).toBe(0)
  })
})

/** Step 15 (b)'s payload rules, decision 18. The observer and its debounce
 * are wiring and live in `Markets.tsx`; everything that decides *what may
 * be sent* and *whether it is news* is here, because a hint the engine
 * refuses is a hint lost whole. */
describe('the viewport hint payload', () => {
  it('debounces at the foreground poll interval', () => {
    // The same question as the poll — how often may this page cost the
    // server something — so the same number. A trailing-edge debounce at
    // 400ms sends nothing while a scroll is in progress and one message
    // once it stops; every resubscribe is a gap in the marks.
    expect(MARKETS_VIEWPORT_DEBOUNCE_MS).toBe(400)
  })

  it('mirrors the engine bound at 64', () => {
    expect(MAX_MARKETS_VISIBLE_SYMBOLS).toBe(64)
  })

  it('upper-cases, trims, and keeps DOM order', () => {
    expect(marketsVisibleHint([' nvda ', 'spy', 'BRK.B'])).toEqual(['NVDA', 'SPY', 'BRK.B'])
  })

  it('truncates at the bound rather than sending a list refused whole', () => {
    // The message is applied whole or not at all, so a 65th entry does not
    // cost the 65th row — it costs the hint. The kept rows are the first
    // ones in DOM order, which is where the eye is.
    const many = Array.from({ length: 80 }, (_, i) => `SYM${i}`)
    const hint = marketsVisibleHint(many)
    expect(hint).toHaveLength(MAX_MARKETS_VISIBLE_SYMBOLS)
    expect(hint[0]).toBe('SYM0')
    expect(hint.at(-1)).toBe('SYM63')
  })

  it('drops a symbol wider than the engine admits, without dropping the message', () => {
    // Two validators, and the engine's `_EQUITY_TICKER` (16) is narrower
    // than the transport's shape filter (32, because `subscribe` must admit
    // an OCC contract). A 17-character entry passes the socket and is
    // refused by the engine — and the refusal is of the whole message.
    expect(marketsVisibleHint(['AAPL', 'ABCDEFGHIJKLMNOPQ', 'NVDA'])).toEqual(['AAPL', 'NVDA'])
    expect(marketsVisibleHint(['ABCDEFGHIJKLMNOP'])).toEqual(['ABCDEFGHIJKLMNOP'])
  })

  it('drops an OCC contract, including the 16-character kind', () => {
    // The chain table's rows are contracts and are not this message. The
    // long form fails on width; `A241220C00150000` is exactly 16 and a
    // single-letter root, so it passes the equity shape and has to be
    // refused on the OCC shape instead.
    expect(marketsVisibleHint(['NVDA260914C00210000', 'NVDA'])).toEqual(['NVDA'])
    expect(marketsVisibleHint(['A241220C00150000', 'NVDA'])).toEqual(['NVDA'])
  })

  it('drops anything that is not a ticker at all', () => {
    expect(marketsVisibleHint(['', ' ', '1NVDA', 'A B', 'NV-DA', 'NVDA'])).toEqual(['NVDA'])
  })

  it('de-duplicates, first occurrence winning', () => {
    expect(marketsVisibleHint(['NVDA', 'nvda', 'SPY'])).toEqual(['NVDA', 'SPY'])
  })

  it('treats an empty viewport as a legitimate payload', () => {
    expect(marketsVisibleHint([])).toEqual([])
  })

  it('says nothing when nothing is in force and nothing is on screen', () => {
    // Null is "no hint believed to be in force". An empty list against a
    // server that holds none is not news.
    expect(marketsVisibleDiffers(null, [])).toBe(false)
    expect(marketsVisibleDiffers(null, ['NVDA'])).toBe(true)
  })

  it('reads an emptied viewport against a held hint as news', () => {
    // The unmount case: leaving Markets is exactly "nothing is on screen".
    expect(marketsVisibleDiffers(['NVDA'], [])).toBe(true)
  })

  it('compares ordered, the way the server does', () => {
    // `EngineRuntime.set_markets_visible` dedups with `dict.fromkeys` and
    // then compares tuples, so a reorder *is* a change there. A client
    // whose idea of unchanged is wider than the server's suppresses a
    // message the server would have acted on.
    expect(marketsVisibleDiffers(['NVDA', 'SPY'], ['NVDA', 'SPY'])).toBe(false)
    expect(marketsVisibleDiffers(['NVDA', 'SPY'], ['SPY', 'NVDA'])).toBe(true)
    expect(marketsVisibleDiffers(['NVDA'], ['NVDA', 'SPY'])).toBe(true)
  })
})
