/**
 * Filtering and ranking for the Markets page (PRD.md §8.4). Pure functions
 * with no React in them, so the ordering rules can be tested without
 * rendering a table — the same split `orders.ts` uses.
 *
 * None of these mutate their input. `OPTION_CHAIN` and `STOCKS` are module
 * singletons and every view reads the same array; an in-place `.sort()`
 * would leave the previous view's ordering behind in the fixture itself.
 */

import { OPTION_CHAIN, type OptionContract, type StockQuote } from './mockData'

// ---------------------------------------------------------------------- //
// Options chains
// ---------------------------------------------------------------------- //

/** The rankings PRD.md §8.4 asks for, plus the chain's own order.
 *
 * `strike` is the default because that is how a chain is actually read —
 * down a ladder, comparing the strike above to the one below. The other
 * five are screens, and answer a different question. */
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

/** Which column a ranking sorts on, so the header can say so. A table
 * sorted by a rule the header does not name looks shuffled. */
export const CHAIN_RANK_COLUMN: Record<ChainRank, string> = {
  strike: 'strike',
  volume: 'volume',
  gainers: 'changePct',
  losers: 'changePct',
  iv: 'iv',
  openInterest: 'openInterest',
}

/** Which way each ranking runs. Not decorative: the header announces it
 * through `aria-sort`, and hardcoding "descending" would tell a screen
 * reader the strike ladder counts down while it visibly counts up. */
export const CHAIN_RANK_DIRECTION: Record<ChainRank, 'ascending' | 'descending'> = {
  strike: 'ascending',
  volume: 'descending',
  gainers: 'descending',
  // Most negative first — the losers screen sorts up, not down.
  losers: 'ascending',
  iv: 'descending',
  openInterest: 'descending',
}

/** Every underlying with a listed chain, in the order the fixture defines
 * them. Derived rather than hardcoded — a second list would drift. */
export const CHAIN_UNDERLYINGS: string[] = [...new Set(OPTION_CHAIN.map((c) => c.symbol))]

/** Minimum contract volume. PRD.md §8.4 lists volume as a *filter*
 * alongside the rankings, and it is the one control here that can empty
 * the table — the thin end of a quiet name genuinely has nothing above
 * 20,000, and saying so is more useful than showing five illiquid strikes
 * as though they were tradeable. */
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

/** Calls before puts at the same strike, which is how a chain is printed. */
const RIGHT_ORDER: Record<OptionContract['type'], number> = { call: 0, put: 1 }

export function rankChain(contracts: OptionContract[], rank: ChainRank): OptionContract[] {
  const rows = [...contracts]

  switch (rank) {
    case 'strike':
      // Symbol, then expiration, then strike, then right. Sorting by
      // strike alone interleaves three expirations of the same underlying
      // into one ladder that does not exist.
      return rows.sort(
        (a, b) =>
          a.symbol.localeCompare(b.symbol) ||
          a.expiration.localeCompare(b.expiration) ||
          a.strike - b.strike ||
          RIGHT_ORDER[a.type] - RIGHT_ORDER[b.type],
      )

    case 'volume':
      return rows.sort((a, b) => b.volume - a.volume)

    case 'openInterest':
      return rows.sort((a, b) => b.openInterest - a.openInterest)

    case 'iv':
      return rows.sort((a, b) => b.iv - a.iv)

    case 'gainers':
      // Biggest gain first. Losers is not the reverse of this list — it is
      // the same list read from the other end, and reversing `gainers`
      // would put the flattest contracts on top of the losers screen.
      return rows.sort((a, b) => b.changePct - a.changePct)

    case 'losers':
      return rows.sort((a, b) => a.changePct - b.changePct)
  }
}

// ---------------------------------------------------------------------- //
// Stocks & ETFs
// ---------------------------------------------------------------------- //

export type StockRank = 'active' | 'gainers' | 'losers' | 'new' | 'marketCap'

export const STOCK_RANKS: StockRank[] = ['active', 'gainers', 'losers', 'new', 'marketCap']

export const STOCK_RANK_LABEL: Record<StockRank, string> = {
  active: 'Most active',
  gainers: 'Top gainers',
  losers: 'Top losers',
  new: 'New listings',
  marketCap: 'Market cap',
}

export const STOCK_RANK_COLUMN: Record<StockRank, string> = {
  active: 'volume',
  gainers: 'changePct',
  losers: 'changePct',
  new: 'listedOn',
  marketCap: 'marketCap',
}

export const STOCK_RANK_DIRECTION: Record<StockRank, 'ascending' | 'descending'> = {
  active: 'descending',
  gainers: 'descending',
  losers: 'ascending',
  // Most recently listed first, so the newest date is at the top.
  new: 'descending',
  marketCap: 'descending',
}

export function rankStocks(stocks: StockQuote[], rank: StockRank): StockQuote[] {
  const rows = [...stocks]

  switch (rank) {
    case 'active':
      return rows.sort((a, b) => b.volume - a.volume)

    case 'gainers':
      return rows.sort((a, b) => b.changePct - a.changePct)

    case 'losers':
      return rows.sort((a, b) => a.changePct - b.changePct)

    case 'new':
      // Most recently listed first.
      return rows.sort((a, b) => b.listedOn.localeCompare(a.listedOn))

    case 'marketCap':
      // A fund has no market capitalisation, so it sorts to the bottom
      // rather than being coerced to zero. Coercing would rank SPY below
      // the smallest company on the list and state, in a column of
      // dollars, that it is worth nothing.
      return rows.sort((a, b) => {
        if (a.marketCap === null && b.marketCap === null) return a.symbol.localeCompare(b.symbol)
        if (a.marketCap === null) return 1
        if (b.marketCap === null) return -1
        return b.marketCap - a.marketCap
      })
  }
}
