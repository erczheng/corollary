/**
 * Filtering, ranking and sorting for the Markets page (PRD.md §8.4). Pure
 * functions with no React in them, so the ordering rules can be tested
 * without rendering a table — the same split `orders.ts` uses.
 *
 * None of these mutate their input. The chain and the stock universe are
 * single arrays that every view reads; an in-place `.sort()` would leave
 * the previous view's ordering behind in the data itself.
 */

import {
  OPTION_CHAIN,
  type OptionContract,
  type StockQuote,
  type UnderlyingQuote,
} from './mockData'

export type SortDirection = 'ascending' | 'descending'

/** Nulls sort last in **both** directions, rather than being coerced to a
 * number. An absent market capitalisation is not a small one: coerced to
 * zero, SPY ranks below the smallest company on the list and a column of
 * dollars states that a fund is worth nothing. Descending or ascending, a
 * row with no value belongs at the bottom either way. */
function compareNullable(a: number | null, b: number | null, direction: SortDirection): number | null {
  if (a === null && b === null) return 0
  if (a === null) return 1
  if (b === null) return -1
  return direction === 'ascending' ? a - b : b - a
}

function compare(a: number, b: number, direction: SortDirection): number {
  return direction === 'ascending' ? a - b : b - a
}

// ---------------------------------------------------------------------- //
// Options chains
// ---------------------------------------------------------------------- //

/** `ladder` is the chain's own order — symbol, expiration, strike, calls
 * before puts — and it is not a column you can click. The rest are, and
 * they are exactly the quote columns: sorting by Type would group two
 * halves of the same ladder into blocks that no longer read as a chain,
 * and sorting by Strike across three expirations interleaves ladders that
 * do not exist. */
export type ChainSortKey =
  | 'ladder'
  | 'last'
  | 'change'
  | 'changePct'
  | 'bid'
  | 'ask'
  | 'volume'
  | 'openInterest'
  | 'iv'

export interface ChainSort {
  key: ChainSortKey
  direction: SortDirection
}

/** Which way a column wants to be read the first time you click it. Money
 * and size open descending — the question is "what is biggest" — and there
 * is no column here where smallest-first is the obvious first ask. */
export const CHAIN_DEFAULT_DIRECTION: SortDirection = 'descending'

export const CHAIN_LADDER_SORT: ChainSort = { key: 'ladder', direction: 'ascending' }

/** Calls before puts at the same strike, which is how a chain is printed. */
const RIGHT_ORDER: Record<OptionContract['type'], number> = { call: 0, put: 1 }

export function sortChain(contracts: OptionContract[], sort: ChainSort): OptionContract[] {
  const rows = [...contracts]

  if (sort.key === 'ladder') {
    // Symbol, then expiration, then strike, then right. Sorting by strike
    // alone interleaves three expirations of the same underlying into one
    // ladder that does not exist.
    return rows.sort(
      (a, b) =>
        a.symbol.localeCompare(b.symbol) ||
        a.expiration.localeCompare(b.expiration) ||
        a.strike - b.strike ||
        RIGHT_ORDER[a.type] - RIGHT_ORDER[b.type],
    )
  }

  const key = sort.key
  // Ties keep the ladder's order rather than whatever the array happened to
  // hold, so two contracts on the same volume don't swap places between
  // renders.
  const ladder = sortChain(rows, CHAIN_LADDER_SORT)
  return ladder.sort((a, b) => compare(a[key], b[key], sort.direction))
}

/** The named screens PRD.md §8.4 asks for, expressed as sorts. The
 * dropdown and the column headers drive one piece of state between them —
 * two independent sorts would let the header say one thing while the
 * dropdown claimed another. */
export type ChainRank = 'strike' | 'volume' | 'gainers' | 'losers' | 'iv' | 'openInterest'

export const CHAIN_RANKS: ChainRank[] = ['strike', 'volume', 'gainers', 'losers', 'iv', 'openInterest']

export const CHAIN_RANK_LABEL: Record<ChainRank, string> = {
  strike: 'Strike ladder',
  volume: 'Most volume',
  gainers: 'Top gainers',
  losers: 'Top losers',
  iv: 'Highest IV',
  openInterest: 'Highest open interest',
}

export const CHAIN_RANK_SORT: Record<ChainRank, ChainSort> = {
  strike: CHAIN_LADDER_SORT,
  volume: { key: 'volume', direction: 'descending' },
  gainers: { key: 'changePct', direction: 'descending' },
  // Most negative first — the losers screen sorts up, not down, and is not
  // the gainers list reversed.
  losers: { key: 'changePct', direction: 'ascending' },
  iv: { key: 'iv', direction: 'descending' },
  openInterest: { key: 'openInterest', direction: 'descending' },
}

/** The screen a sort corresponds to, or null if clicking a header has taken
 * the table somewhere no preset describes. The dropdown shows that as
 * "Custom" rather than keeping a stale label from the last preset. */
export function chainRankFor(sort: ChainSort): ChainRank | null {
  const match = CHAIN_RANKS.find(
    (r) => CHAIN_RANK_SORT[r].key === sort.key && CHAIN_RANK_SORT[r].direction === sort.direction,
  )
  return match ?? null
}

/** Every underlying with a listed chain, in the order the fixture defines
 * them. Derived rather than hardcoded — a second list would drift. */
export const CHAIN_UNDERLYINGS: string[] = [...new Set(OPTION_CHAIN.map((c) => c.symbol))]

/** Minimum contract volume. PRD.md §8.4 lists volume as a *filter*
 * alongside the rankings, and it is the one control here that can empty the
 * table — the thin end of a quiet name genuinely has nothing above 20,000,
 * and saying so is more useful than showing five illiquid strikes as though
 * they were tradeable. */
export const MIN_VOLUME_STEPS = [0, 5_000, 20_000, 50_000]

export interface ChainFilter {
  /** A symbol, or `null` for every underlying at once. */
  underlying: string | null
  minVolume: number
}

export function filterChain(contracts: OptionContract[], filter: ChainFilter): OptionContract[] {
  return contracts.filter(
    (c) =>
      (filter.underlying === null || c.symbol === filter.underlying) && c.volume >= filter.minVolume,
  )
}

/** Substring match on the symbol, for the underlying search. Case
 * insensitive and unanchored: someone typing "qq" is looking for QQQ, and
 * someone typing "sp" should not have to know whether the list is
 * alphabetical. */
export function searchUnderlyings(symbols: string[], query: string): string[] {
  const q = query.trim().toLowerCase()
  if (q === '') return symbols
  return symbols.filter((s) => s.toLowerCase().includes(q))
}

/** A contract's display name, in the same shape the rest of the app writes
 * contracts in — `AAPL $230 Call Aug 21`. One format everywhere, so a
 * contract bought from the chain reads identically in the ledger. */
export function contractName(c: OptionContract, expiryLabel: string): string {
  return `${c.symbol} $${c.strike} ${c.type === 'call' ? 'Call' : 'Put'} ${expiryLabel}`
}

// ---------------------------------------------------------------------- //
// Stocks & ETFs
// ---------------------------------------------------------------------- //

export type StockSortKey = 'price' | 'change' | 'changePct' | 'volume' | 'relVolume' | 'marketCap'

export interface StockSort {
  key: StockSortKey
  direction: SortDirection
}

/** Today's volume against the name's average.
 *
 * This is what "trending now" means on a screener, and it is a different
 * question from "most active": raw volume finds the same mega caps every
 * session, because NVDA trades 200M shares on a quiet day. Relative volume
 * finds the name that is doing something unusual *for itself*, which is
 * the one worth looking at. */
export function relativeVolume(s: StockQuote): number {
  return s.avgVolume === 0 ? 0 : s.volume / s.avgVolume
}

export function sortStocks(stocks: StockQuote[], sort: StockSort): StockQuote[] {
  const rows = [...stocks].sort((a, b) => a.symbol.localeCompare(b.symbol))

  if (sort.key === 'marketCap') {
    return rows.sort((a, b) => compareNullable(a.marketCap, b.marketCap, sort.direction) ?? 0)
  }
  if (sort.key === 'relVolume') {
    return rows.sort((a, b) => compare(relativeVolume(a), relativeVolume(b), sort.direction))
  }

  const key = sort.key
  return rows.sort((a, b) => compare(a[key], b[key], sort.direction))
}

export type StockRank = 'active' | 'trending' | 'gainers' | 'losers' | 'marketCap'

export const STOCK_RANKS: StockRank[] = ['active', 'trending', 'gainers', 'losers', 'marketCap']

export const STOCK_RANK_LABEL: Record<StockRank, string> = {
  active: 'Most active',
  trending: 'Trending now',
  gainers: 'Top gainers',
  losers: 'Top losers',
  marketCap: 'Market cap',
}

export const STOCK_RANK_SORT: Record<StockRank, StockSort> = {
  active: { key: 'volume', direction: 'descending' },
  trending: { key: 'relVolume', direction: 'descending' },
  gainers: { key: 'changePct', direction: 'descending' },
  losers: { key: 'changePct', direction: 'ascending' },
  marketCap: { key: 'marketCap', direction: 'descending' },
}

export function stockRankFor(sort: StockSort): StockRank | null {
  const match = STOCK_RANKS.find(
    (r) => STOCK_RANK_SORT[r].key === sort.key && STOCK_RANK_SORT[r].direction === sort.direction,
  )
  return match ?? null
}

/** Re-quotes the universe from the live price map.
 *
 * Price, change and percent are not stored on the row — they belong to the
 * symbol, and the symbol has exactly one quote. A stock carrying its own
 * copy is how the Markets table and an Activity row end up disagreeing
 * about what AAPL costs. */
export function liveStocks(
  base: StockQuote[],
  quotes: Record<string, UnderlyingQuote>,
): StockQuote[] {
  return base.map((s) => {
    const q = quotes[s.symbol]
    if (!q) return s
    return { ...s, price: q.price, change: q.change, changePct: q.changePct }
  })
}
