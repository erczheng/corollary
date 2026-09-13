/**
 * Filtering, ranking and sorting for the Markets page (PRD.md §8.4). Pure
 * functions with no React in them, so the ordering rules can be tested
 * without rendering a table — the same split `orders.ts` uses.
 *
 * None of these mutate their input. The chain and the stock universe are
 * single arrays that every view reads; an in-place `.sort()` would leave
 * the previous view's ordering behind in the data itself.
 *
 * **Every market figure here is nullable, and none of them is coerced.** A
 * real chain has no bid on half its contracts, no previous close on a
 * newly listed one, and no open interest at all on this data plan. A null
 * sorts *last in both directions* and renders as an absence — the rule the
 * fund `marketCap` column has always followed, now applied to the columns
 * the live feed turned out to share it with.
 */

import { type OptionContract, type StockQuote } from './types'

export type SortDirection = 'ascending' | 'descending'

/** Nulls sort last in **both** directions, rather than being coerced to a
 * number. An absent market capitalisation is not a small one: coerced to
 * zero, SPY ranks below the smallest company on the list and a column of
 * dollars states that a fund is worth nothing. Descending or ascending, a
 * row with no value belongs at the bottom either way. */
function compareNullable(a: number | null, b: number | null, direction: SortDirection): number {
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

/** Which solver produced a contract's implied volatility.
 *
 * Decision 10: Alpaca serves an IV on the indicative feed only where its
 * own Black-Scholes solve succeeded, and Corollary derives the rest from
 * the mid. A chain that silently mixed the two would be worse than either
 * alone, so the number carries its provenance and the column says which.
 *
 * Deliberately **not** a field on `OptionContract`. The server sends
 * `iv_source`, and `tests/api/test_schema_contract.py` lists it as an
 * addition `types.ts` must not declare; declaring it fails that test. Read
 * structurally instead, which is honest about where it comes from. */
export type AnalyticsSource = 'vendor' | 'derived'

export function ivSourceOf(contract: OptionContract): AnalyticsSource | null {
  const raw = (contract as OptionContract & { ivSource?: unknown }).ivSource
  return raw === 'vendor' || raw === 'derived' ? raw : null
}

/** `ladder` is the chain's own order — symbol, expiration, strike, calls
 * before puts — and it is not a column you can click. The rest are, and
 * they are exactly the quote columns: sorting by Type would group two
 * halves of the same ladder into blocks that no longer read as a chain,
 * and sorting by Strike across three expirations interleaves ladders that
 * do not exist.
 *
 * **`openInterest` is absent, and that is decision 15.** Open interest has
 * no permissible free source on this plan, so the column is present but
 * mostly empty and a sort on it would be a ranking over nulls — the
 * invented-number failure PRD §8.5 exists to prevent. A ranking option
 * that returns the list unsorted is worse than an absent one, the same
 * reason the command palette omits Execute while halted rather than
 * showing it disabled. It comes back when the data does. */
export type ChainSortKey =
  | 'ladder'
  | 'last'
  | 'change'
  | 'changePct'
  | 'bid'
  | 'ask'
  | 'volume'
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
    // Expiration, then strike, then right. Sorting by strike alone
    // interleaves three expirations of the same underlying into one ladder
    // that does not exist.
    //
    // **Not `symbol` first.** On the wire `symbol` is the *OCC contract*
    // symbol -- `NVDA260914C00210000` -- not the underlying, and it encodes
    // the right *before* the strike. Sorting on it first would group every
    // call above every put and stop the ladder reading as a ladder. It
    // stays only as a final tiebreak, where it is a stable identity.
    return rows.sort(
      (a, b) =>
        a.expiration.localeCompare(b.expiration) ||
        a.strike - b.strike ||
        RIGHT_ORDER[a.type] - RIGHT_ORDER[b.type] ||
        a.symbol.localeCompare(b.symbol),
    )
  }

  const key = sort.key
  // Ties keep the ladder's order rather than whatever the array happened to
  // hold, so two contracts on the same volume don't swap places between
  // renders. Contracts with nothing in the column tie with each other and
  // settle to the ladder at the bottom of the table.
  const ladder = sortChain(rows, CHAIN_LADDER_SORT)
  return ladder.sort((a, b) => compareNullable(a[key], b[key], sort.direction))
}

/** The named screens PRD.md §8.4 asks for, expressed as sorts. The
 * dropdown and the column headers drive one piece of state between them —
 * two independent sorts would let the header say one thing while the
 * dropdown claimed another.
 *
 * *"Highest open interest"* was one of these and is **removed** — see
 * `ChainSortKey`. */
export type ChainRank = 'strike' | 'volume' | 'gainers' | 'losers' | 'iv'

export const CHAIN_RANKS: ChainRank[] = ['strike', 'volume', 'gainers', 'losers', 'iv']

export const CHAIN_RANK_LABEL: Record<ChainRank, string> = {
  strike: 'Strike ladder',
  volume: 'Most volume',
  gainers: 'Top gainers',
  losers: 'Top losers',
  iv: 'Highest IV',
}

export const CHAIN_RANK_SORT: Record<ChainRank, ChainSort> = {
  strike: CHAIN_LADDER_SORT,
  volume: { key: 'volume', direction: 'descending' },
  gainers: { key: 'changePct', direction: 'descending' },
  // Most negative first — the losers screen sorts up, not down, and is not
  // the gainers list reversed.
  losers: { key: 'changePct', direction: 'ascending' },
  iv: { key: 'iv', direction: 'descending' },
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

/** The underlyings the chain combobox offers, from the quoted universe the
 * server serves rather than a second list beside it.
 *
 * Phase 1 derived this from the fixture chain, which could answer "does
 * this name have a chain" without asking. Live, that question costs a
 * request per symbol, so it is not asked: every quoted name is offered and
 * the chain's own empty state answers for the ones with nothing listed. */
export function underlyingSymbols(stocks: StockQuote[]): string[] {
  return [...new Set(stocks.map((s) => s.symbol))].sort((a, b) => a.localeCompare(b))
}

/** Minimum contract volume. PRD.md §8.4 lists volume as a *filter*
 * alongside the rankings, and it is the one control here that can empty the
 * table — the thin end of a quiet name genuinely has nothing above 20,000,
 * and saying so is more useful than showing five illiquid strikes as though
 * they were tradeable. */
export const MIN_VOLUME_STEPS = [0, 5_000, 20_000, 50_000]

/** Volume, and nothing else.
 *
 * The underlying used to be filtered here, because Phase 1 held every
 * chain in one fixture array and `OptionContract.symbol` was the
 * underlying. Live, the chain is **fetched** one underlying at a time and
 * `symbol` is the OCC contract symbol, so filtering on it again would
 * compare `NVDA260914C00210000` against `NVDA` and empty the table. */
export interface ChainFilter {
  minVolume: number
}

/** A contract with **no** volume is not a contract with low volume, so it
 * survives "Any volume" and fails every floor above it. Coercing the null
 * to 0 would give the same answer, and would also be a claim that it did
 * not trade; writing the branch out says which question is being asked. */
export function filterChain(contracts: OptionContract[], filter: ChainFilter): OptionContract[] {
  return contracts.filter((c) => {
    if (filter.minVolume === 0) return true
    return c.volume !== null && c.volume >= filter.minVolume
  })
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
 * the one worth looking at.
 *
 * **Null rather than 0 when either side is missing**, and the denominator
 * is the load-bearing one: a 0 average is a division by zero, so a symbol
 * whose history could not be fetched would sort *first* on the screen
 * built to find unusual activity. Both figures come from the same
 * historical feed server-side, and that sameness is the field — an IEX
 * numerator over a SIP denominator read 0.032 across 26 symbols where the
 * same-feed figure is 0.816, and every name on the screen looked dead. */
export function relativeVolume(s: StockQuote): number | null {
  if (s.volume === null || s.avgVolume === null || s.avgVolume === 0) return null
  return s.volume / s.avgVolume
}

export function sortStocks(stocks: StockQuote[], sort: StockSort): StockQuote[] {
  const rows = [...stocks].sort((a, b) => a.symbol.localeCompare(b.symbol))

  if (sort.key === 'relVolume') {
    return rows.sort((a, b) => compareNullable(relativeVolume(a), relativeVolume(b), sort.direction))
  }
  if (sort.key === 'price') {
    // The one column that cannot be null: a symbol with no price is not
    // served as a row at all, because there is nothing to draw.
    return rows.sort((a, b) => compare(a.price, b.price, sort.direction))
  }

  const key = sort.key
  return rows.sort((a, b) => compareNullable(a[key], b[key], sort.direction))
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

/** Substring match over symbol and name, for the stock search. Both,
 * because half the reason to search a screener is that you know the company
 * and not the ticker — "reddit" should find RDDT. */
export function searchStocks(stocks: StockQuote[], query: string): StockQuote[] {
  const q = query.trim().toLowerCase()
  if (q === '') return stocks
  return stocks.filter(
    (s) => s.symbol.toLowerCase().includes(q) || s.name.toLowerCase().includes(q),
  )
}

// ---------------------------------------------------------------------- //
// Which session a volume figure covers
// ---------------------------------------------------------------------- //

/** What the number in the Volume column actually counts.
 *
 * - `partial` — traded so far in a session that is still running.
 * - `session` — a completed session, and the most recent one on the table.
 * - `stale` — a completed session that is **not** the latest one anybody
 *   here printed in. The symbol stopped printing and the column should say
 *   so rather than quietly showing an old number as though it were today's.
 * - `absent` — no daily bar in the window at all. Not a zero.
 *
 * The server resolves the session, and reading it back from
 * `new Date()` would be reading a browser-local clock against New York
 * boundaries. The one comparison made here is between two served dates. */
export type VolumeBasis = 'partial' | 'session' | 'stale' | 'absent'

/** The most recent session any served row printed in, as `YYYY-MM-DD`, or
 * null if none did.
 *
 * Derived from the response rather than from the clock: ISO dates compare
 * lexicographically, so this is a max over what the server said, and it
 * stays correct on a weekend, a holiday, and at 09:31 when half the table
 * has printed and half has not. */
export function latestVolumeDate(stocks: StockQuote[]): string | null {
  let latest: string | null = null
  for (const s of stocks) {
    // `!= null`, not `!== null`: an older server omits the key entirely, and
    // `undefined !== null` is true, so a strict check assigns `undefined` to
    // `latest` and this returns it under a `string | null` annotation.
    if (s.volumeDate != null && (latest === null || s.volumeDate > latest)) latest = s.volumeDate
  }
  return latest
}

export function volumeBasis(s: StockQuote, latestDate: string | null): VolumeBasis {
  // Every guard here is `== null`, never `=== null`. These fields are nullable
  // by contract, but they are also *absent* whenever the server predates the
  // commit that added them -- and `request<StockQuote[]>` casts unvalidated
  // JSON, so the types promise a shape the wire does not enforce. Under a
  // strict check `undefined` slipped past all three and fell through to
  // `'session'`, so a 10:30 partial rendered as "full session": NVDA at 30M of
  // a 126M day, relative volume 0.24x, a mega cap reading dead quiet. That is
  // the exact failure the volume-session feature was built to prevent, and it
  // was silent -- unlike the crash it replaced.
  if (s.volume == null || s.volumeSession == null) return 'absent'
  if (s.volumeSession === 'in_progress') return 'partial'
  if (s.volumeDate != null && latestDate != null && s.volumeDate < latestDate) return 'stale'
  return 'session'
}

export interface Moneyness {
  /** In the money — the contract has intrinsic value at this spot. */
  itm: boolean
  /** Absolute distance from spot to strike, in dollars. Unsigned: which
   * way it points is what `itm` already says, and a signed figure would
   * mean opposite things on a call and a put. */
  distance: number
}

/** Where the stock sits relative to the strike.
 *
 * The one fact an option ticket cannot omit: it decides what the contract
 * is worth at expiry. **A call is in the money above its strike and a put
 * is in the money below it** — inverted, a ticket would tell you a put was
 * worthless at exactly the moment it was worth the most. At the strike
 * exactly it is *out* of the money, because intrinsic value is zero. */
export function contractMoneyness(
  spot: number,
  strike: number,
  right: 'call' | 'put',
): Moneyness {
  return {
    itm: right === 'call' ? spot > strike : spot < strike,
    distance: Math.abs(spot - strike),
  }
}
