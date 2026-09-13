import { Fragment, useState } from 'react'
import { Pagination } from '../components/Pagination'
import { RefreshButton } from '../components/RefreshButton'
import { RequestFailed } from '../components/RequestFailed'
import { SymbolCombobox } from '../components/SymbolCombobox'
import { ChainOrderTicket } from '../components/ChainOrderTicket'
import { UnderlyingChart } from '../components/UnderlyingChart'
import { TableSkeleton } from '../components/Skeleton'
import { usePagination } from '../hooks/usePagination'
import { useUIStore } from '../lib/store'
import { useAccount, useChain, useStocks } from '../lib/queries'
import { isAccountUnavailable, isApiError } from '../lib/api'
import { type OptionContract, type StockQuote } from '../lib/types'
import {
  CHAIN_DEFAULT_DIRECTION,
  CHAIN_LADDER_SORT,
  CHAIN_RANKS,
  CHAIN_RANK_LABEL,
  CHAIN_RANK_SORT,
  MIN_VOLUME_STEPS,
  STOCK_RANKS,
  STOCK_RANK_LABEL,
  STOCK_RANK_SORT,
  chainRankFor,
  filterChain,
  ivSourceOf,
  latestVolumeDate,
  relativeVolume,
  searchStocks,
  sortChain,
  sortStocks,
  stockRankFor,
  underlyingSymbols,
  volumeBasis,
  type ChainRank,
  type ChainSort,
  type ChainSortKey,
  type SortDirection,
  type StockRank,
  type StockSort,
  type StockSortKey,
  type VolumeBasis,
} from '../lib/markets'
import {
  formatCompactNumber,
  formatExpiry,
  formatInteger,
  formatIv,
  formatMarketCap,
  formatPct,
  formatTimeET,
  formatUsd,
  signClass,
} from '../lib/format'

/** Same depth as Activity's ledger — deep enough that pagination is doing
 * real work, short enough that neither table becomes its own scroll
 * region. */
const PAGE_SIZE = 15

/** The chain section's anchor, so the stock table can send you to it. */
const CHAIN_SECTION_ID = 'options-chains'

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'
const TD_NUM = `${TD} whitespace-nowrap text-right text-data-md text-on-surface`
/** Trade and Chart — this page's two row actions. 24px inside a 32px row,
 * and `flex w-fit` rather than inline because a lone inline-level control
 * in a cell rides the cell's text baseline and brings a descender's worth
 * of strut with it. `ml-auto` right-aligns it without a wrapper. */
const ROW_BUTTON =
  'ml-auto flex h-6 w-fit items-center rounded border border-outline px-2 text-label-sm text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container'

const SELECT =
  'rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface focus:border-primary'
const SEARCH =
  // w-56, not w-44: the placeholder is where the field says it covers
  // company names too, which is the non-obvious half of what it does, and
  // at w-44 it truncated to 'Search symbol or na'.
  'w-56 rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface placeholder:text-on-surface-variant focus:border-primary'

/** A figure the feed did not carry.
 *
 * An em dash rather than a blank cell, and never a `0` — on this page zero
 * is a real and different fact. Open interest of 0 means nobody holds the
 * contract; open interest *absent* means nobody may tell us, which is
 * decision 15. The same rule the fund market-cap column has always
 * followed, now shared with every column the live feed turned out to leave
 * empty. */
function Unavailable({ reason }: { reason: string }) {
  return (
    <span className="text-on-surface-variant" title={reason}>
      —<span className="sr-only"> unavailable: {reason}</span>
    </span>
  )
}

/** A right-aligned numeric cell that renders an absence as an absence. */
function NumCell({
  value,
  format,
  reason,
  className = '',
}: {
  value: number | null
  format: (n: number) => string
  reason: string
  className?: string
}) {
  return (
    <td className={`${TD_NUM} ${className}`}>
      {value === null ? <Unavailable reason={reason} /> : format(value)}
    </td>
  )
}

interface Column<K extends string> {
  key: string
  label: string
  align: 'left' | 'right'
  /** Sortable columns are the quote columns and nothing else. Sorting by
   * Type would split a ladder into two blocks that no longer read as a
   * chain, and Strike across three expirations interleaves ladders that do
   * not exist — those are identity, not order.
   *
   * **OI is not sortable either, and that is decision 15 rather than
   * taste**: leaving the header clickable would restore the removed
   * "highest open interest" ranking by another route, over a column that
   * is mostly null. */
  sortKey?: K
  /** Absorbs the table's slack so the neighbours stay tight to their
   * content. At most one per table, and only where a column genuinely
   * should take the width — a stock's Name. */
  grow?: boolean
}

/** A header row where the quote columns are buttons.
 *
 * The caret and `aria-sort` both report the real direction rather than a
 * hardcoded one: the strike ladder counts up and the losers screen sorts up
 * from the worst, and a header claiming "descending" over an ascending
 * column is worse than a header that says nothing at all. */
function SortableHead<K extends string>({
  columns,
  sort,
  onSort,
}: {
  columns: Column<K>[]
  sort: { key: string; direction: SortDirection }
  onSort: (key: K) => void
}) {
  return (
    <thead>
      <tr className="bg-surface-container">
        {columns.map((c) => {
          const sorted = c.sortKey !== undefined && c.sortKey === sort.key
          return (
            <th
              key={c.key}
              aria-sort={sorted ? sort.direction : undefined}
              className={`${TH} ${c.align === 'right' ? 'text-right' : 'text-left'} ${
                c.grow ? 'w-full' : ''
              } ${sorted ? 'text-on-surface' : ''}`}
            >
              {c.sortKey === undefined ? (
                c.label
              ) : (
                <button
                  type="button"
                  onClick={() => onSort(c.sortKey as K)}
                  className="uppercase transition-colors duration-base ease-standard hover:text-on-surface"
                >
                  {c.label}
                  {/* The caret's width is reserved whether or not this is
                      the sorted column, so clicking a header does not
                      shift every column beside it by a glyph. */}
                  <span aria-hidden="true" className="ml-1 inline-block w-2">
                    {sorted ? (sort.direction === 'ascending' ? '▴' : '▾') : ''}
                  </span>
                </button>
              )}
            </th>
          )
        })}
      </tr>
    </thead>
  )
}

/** Signed money or signed percent. Every change-shaped number in this app
 * carries an explicit + or − as well as its colour — colour alone fails in
 * grayscale, in a screenshot, and for a colourblind reader.
 *
 * Null is an absence, not a flat day: with no previous close there is
 * nothing to measure the move from, and a `+$0.00` would claim the price
 * was unchanged. */
function SignedCell({
  value,
  percent,
  reason,
}: {
  value: number | null
  percent?: boolean
  reason: string
}) {
  if (value === null) {
    return (
      <td className={`${TD} whitespace-nowrap text-right text-data-md`}>
        <Unavailable reason={reason} />
      </td>
    )
  }
  return (
    <td className={`${TD} whitespace-nowrap text-right text-data-md ${signClass(value)}`}>
      {percent ? formatPct(value, { signed: true }) : formatUsd(value, { signed: true })}
    </td>
  )
}

/** Clicking the sorted column flips it; clicking a new one starts in the
 * direction that column wants to be read. Written once because both tables
 * need exactly the same rule, and two copies of it drift. */
function nextSort<K extends string>(
  current: { key: string; direction: SortDirection },
  key: K,
  fallback: SortDirection,
): { key: K; direction: SortDirection } {
  if (current.key !== key) return { key, direction: fallback }
  return { key, direction: current.direction === 'descending' ? 'ascending' : 'descending' }
}

const NO_PREVIOUS_CLOSE = 'no previous daily bar to measure the move from'

const CHAIN_COLUMNS: Column<ChainSortKey>[] = [
  { key: 'symbol', label: 'Symbol', align: 'left' },
  { key: 'expiration', label: 'Exp', align: 'left' },
  { key: 'type', label: 'Type', align: 'left' },
  { key: 'strike', label: 'Strike', align: 'right' },
  { key: 'last', label: 'Last', align: 'right', sortKey: 'last' },
  { key: 'change', label: 'Change', align: 'right', sortKey: 'change' },
  { key: 'changePct', label: 'Change %', align: 'right', sortKey: 'changePct' },
  { key: 'bid', label: 'Bid', align: 'right', sortKey: 'bid' },
  { key: 'ask', label: 'Ask', align: 'right', sortKey: 'ask' },
  { key: 'volume', label: 'Volume', align: 'right', sortKey: 'volume' },
  // No sortKey. See `Column.sortKey` and decision 15.
  { key: 'openInterest', label: 'OI', align: 'right' },
  { key: 'iv', label: 'IV', align: 'right', sortKey: 'iv' },
  { key: 'trade', label: 'Trade', align: 'right' },
]

const STOCK_COLUMNS: Column<StockSortKey>[] = [
  { key: 'symbol', label: 'Symbol', align: 'left' },
  { key: 'name', label: 'Name', align: 'left', grow: true },
  { key: 'price', label: 'Price', align: 'right', sortKey: 'price' },
  { key: 'change', label: 'Change', align: 'right', sortKey: 'change' },
  { key: 'changePct', label: 'Change %', align: 'right', sortKey: 'changePct' },
  { key: 'volume', label: 'Volume', align: 'right', sortKey: 'volume' },
  { key: 'relVolume', label: 'Rel vol', align: 'right', sortKey: 'relVolume' },
  { key: 'marketCap', label: 'Market cap', align: 'right', sortKey: 'marketCap' },
  { key: 'chart', label: 'Chart', align: 'right' },
]

function contractKeyOf(c: OptionContract): string {
  return `${c.symbol}-${c.expiration}-${c.strike}-${c.type}`
}

/** What the Volume column counts, in words, per row.
 *
 * A column that silently means either "half a Tuesday morning" or "all of
 * Monday" makes a busy stock read as quiet, so every row says which. The
 * session is resolved server-side and never re-derived from `new Date()`:
 * every boundary here is a New York one and the browser's clock is not. */
function volumeNote(basis: VolumeBasis, volumeDate: string | null): string {
  switch (basis) {
    case 'partial':
      return 'so far'
    case 'session':
      return 'full session'
    case 'stale':
      // The date, not "last session": a symbol that stopped printing should
      // be visibly stale rather than quietly old.
      return volumeDate === null ? 'earlier session' : formatExpiry(volumeDate)
    case 'absent':
      return ''
  }
}

function volumeTitle(basis: VolumeBasis, volumeDate: string | null): string {
  const day = volumeDate === null ? 'an unnamed session' : formatExpiry(volumeDate)
  switch (basis) {
    case 'partial':
      return `Traded so far on ${day}, which is still running`
    case 'session':
      return `The complete session of ${day}`
    case 'stale':
      return `The complete session of ${day} — this symbol has not printed since`
    case 'absent':
      return 'no daily bar in the window; this is not a zero'
  }
}

function OptionsChains({
  underlying,
  onUnderlyingChange,
  symbols,
  spot,
  equity,
  equityUnavailable,
}: {
  /** Lifted, because the stock table points this at a symbol too. Two
   * copies of it would let the table say AAPL while the chain showed SPY. */
  underlying: string | null
  onUnderlyingChange: (symbol: string | null) => void
  symbols: string[]
  spot: number | null
  /** Null when the account balance could not be read — the ticket says so
   * rather than quoting a percentage of a number nobody has. */
  equity: number | null
  equityUnavailable: boolean
}) {
  // Disabled until an underlying is chosen: three vendor operations hang
  // off this call, so fetching a chain nobody asked about is not free.
  const chainQuery = useChain(underlying)
  const chain = chainQuery.data ?? []

  const [minVolume, setMinVolume] = useState(0)
  const [sort, setSort] = useState<ChainSort>(CHAIN_LADDER_SORT)
  const [expanded, setExpanded] = useState<string | null>(null)

  const rows = sortChain(filterChain(chain, { minVolume }), sort)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)
  const derivedOnPage = pageItems.some((c) => ivSourceOf(c) === 'derived')

  // The dropdown and the headers drive one sort between them. Clicking a
  // header off a named screen shows "Custom" rather than leaving a stale
  // label claiming the table is still ranked by IV.
  const rank = chainRankFor(sort)

  return (
    <section
      id={CHAIN_SECTION_ID}
      aria-labelledby="options-chains-heading"
      className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
        <div>
          <h2 id="options-chains-heading" className="text-title-lg text-on-surface">
            Options chains
          </h2>
          <p className="mt-1 text-caption text-on-surface-variant">
            {underlying === null
              ? 'No underlying chosen'
              : `${formatInteger(rows.length)} of ${formatInteger(chain.length)} contracts in ${underlying}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SymbolCombobox
            symbols={symbols}
            value={underlying}
            onChange={(next) => {
              onUnderlyingChange(next)
              // The expanded ticket belongs to a row that may not be in the
              // new result set. Leaving it open would put a ticket for a
              // contract you can no longer see under one you can.
              setExpanded(null)
              setPage(1)
            }}
            label="Search underlying"
          />
          <select
            value={minVolume}
            onChange={(e) => {
              setMinVolume(Number(e.target.value))
              setExpanded(null)
              // Back to page one. Narrowing while deep in the chain
              // otherwise lands on the last page of a shorter result set,
              // which reads as "no contracts".
              setPage(1)
            }}
            aria-label="Filter chain by minimum volume"
            className={SELECT}
          >
            {MIN_VOLUME_STEPS.map((v) => (
              <option key={v} value={v}>
                {v === 0 ? 'Any volume' : `Volume ≥ ${formatInteger(v)}`}
              </option>
            ))}
          </select>
          <select
            value={rank ?? 'custom'}
            onChange={(e) => {
              setSort(CHAIN_RANK_SORT[e.target.value as ChainRank])
              setPage(1)
            }}
            aria-label="Rank chain by"
            className={SELECT}
          >
            {rank === null && <option value="custom">Custom sort</option>}
            {CHAIN_RANKS.map((r) => (
              <option key={r} value={r}>
                {CHAIN_RANK_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
      </div>

      {underlying === null ? (
        <p className="px-4 py-6 max-w-prose text-body-md text-on-surface-variant">
          Pick an underlying to read its chain. One name at a time, deliberately: a chain is three
          vendor calls and the whole listed universe at once is a request budget spent on contracts
          nobody asked to see.
        </p>
      ) : chainQuery.isError ? (
        <div className="px-4 py-6">
          <RequestFailed error={chainQuery.error} what={`the ${underlying} chain`} />
        </div>
      ) : chainQuery.isPending ? (
        <TableSkeleton rows={8} columns={CHAIN_COLUMNS.length} label="Loading option chains" />
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 max-w-prose text-body-md text-on-surface-variant">
          {chain.length === 0
            ? `No listed contracts for ${underlying} in the window this page reads. That is an answer about the provider's coverage of this name, not an empty screen.`
            : `Nothing in ${underlying} trades ${formatInteger(minVolume)} contracts or more today. That is an answer about a thin chain rather than an empty screen — lower the volume floor to see what is listed.`}
        </p>
      ) : (
        <>
          {/* Thirteen columns of quotes do not fit a laptop. Scrolling the
              table inside its own panel keeps the page layout intact — the
              alternative, letting it push the page wide, moves every other
              section sideways too. */}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1100px] border-collapse">
              <SortableHead
                columns={CHAIN_COLUMNS}
                sort={sort}
                onSort={(key) => {
                  setSort(nextSort(sort, key, CHAIN_DEFAULT_DIRECTION))
                  setPage(1)
                }}
              />
              <tbody>
                {pageItems.map((c) => {
                  const key = contractKeyOf(c)
                  const open = expanded === key
                  // Buying lifts the ask and selling hits the bid, so an
                  // order needs both. Half of a real chain has neither.
                  const quote = c.bid === null || c.ask === null ? null : { bid: c.bid, ask: c.ask }
                  const ivSource = ivSourceOf(c)
                  return (
                    <Fragment key={key}>
                      <tr
                        className={`h-8 border-t border-outline/10 ${
                          open ? 'bg-surface-container-low' : 'hover:bg-surface-container-low'
                        }`}
                      >
                        <td className={`${TD} text-body-sm text-on-surface`}>{c.symbol}</td>
                        {/* An expiry is a date, not an instant — formatExpiry
                            parses it as UTC. Rendered in ET it would show
                            the day before. */}
                        <td className={`${TD} whitespace-nowrap text-body-sm text-on-surface-variant`}>
                          {formatExpiry(c.expiration)}
                        </td>
                        <td className={`${TD} text-label-md text-on-surface`}>
                          {c.type === 'call' ? 'Call' : 'Put'}
                        </td>
                        <td className={TD_NUM}>{formatUsd(c.strike)}</td>
                        <NumCell value={c.last} format={formatUsd} reason="never traded, and no quote to mark against" />
                        <SignedCell value={c.change} reason={NO_PREVIOUS_CLOSE} />
                        <SignedCell value={c.changePct} percent reason={NO_PREVIOUS_CLOSE} />
                        {/* A null bid is not a zero bid: Alpaca documents
                            `bp: 0` as "the security has no active bid", and
                            it is roughly half of a real ladder. */}
                        <NumCell value={c.bid} format={formatUsd} reason="no active bid" />
                        <NumCell value={c.ask} format={formatUsd} reason="no active offer" />
                        <NumCell value={c.volume} format={formatInteger} reason="no print this session" />
                        {/* Decision 15: open interest has no permissible
                            free source on the Basic plan, so it arrives
                            only where the vendor happened to carry it. A
                            real 0 means nobody holds the contract and is
                            printed as 0; an absence is printed as an
                            absence. The two are different facts. */}
                        <NumCell
                          value={c.openInterest}
                          format={formatInteger}
                          reason="open interest is not available on this data plan"
                        />
                        <td className={TD_NUM}>
                          {c.iv === null ? (
                            <Unavailable reason="no implied volatility could be solved for this contract" />
                          ) : (
                            <span
                              className={ivSource === 'derived' ? 'text-on-surface-variant' : ''}
                              title={
                                ivSource === 'derived'
                                  ? 'Derived locally from the mid, where the vendor returned no solve'
                                  : 'From the vendor'
                              }
                            >
                              {formatIv(c.iv)}
                              <span aria-hidden="true" className="ml-0.5 inline-block w-2 text-caption">
                                {ivSource === 'derived' ? '*' : ''}
                              </span>
                              {ivSource === 'derived' && <span className="sr-only"> derived</span>}
                            </span>
                          )}
                        </td>
                        <td className={`${TD} whitespace-nowrap text-right`}>
                          {quote === null ? (
                            // Omitted rather than shown disabled, the same
                            // rule the command palette follows: there is no
                            // price to write an order against, and a control
                            // that silently does nothing is worse than one
                            // that isn't there.
                            <span className="text-caption text-on-surface-variant" title="No two-sided quote">
                              No quote
                            </span>
                          ) : (
                            <button
                              type="button"
                              aria-expanded={open}
                              /* The expiry is part of the name because a
                                 strike and a right are not unique — the same
                                 $410 call is listed on three expirations, and
                                 without it a screen reader hears the same
                                 button three times. */
                              aria-label={`${open ? 'Close' : 'Trade'} ${c.symbol} ${c.strike} ${c.type} ${formatExpiry(c.expiration)}`}
                              onClick={() => setExpanded(open ? null : key)}
                              className={ROW_BUTTON}
                            >
                              {open ? 'Close' : 'Trade'}
                            </button>
                          )}
                        </td>
                      </tr>
                      {open && quote !== null && (
                        <tr>
                          <td colSpan={CHAIN_COLUMNS.length} className="p-0">
                            <ChainOrderTicket
                              contract={c}
                              quote={quote}
                              spot={spot}
                              equity={equity}
                              equityUnavailable={equityUnavailable}
                              onDone={() => setExpanded(null)}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
          {derivedOnPage && (
            /* Decision 10: IV is derived locally wherever Alpaca's own
               Black-Scholes solve returned nothing, and a derived number
               must never render as though it were measured. */
            <p className="border-t border-outline-variant px-4 py-2 text-caption text-on-surface-variant">
              <span aria-hidden="true">* </span>
              Implied volatility derived locally from the contract's mid, where the vendor returned
              no solve of its own. Unmarked figures are the vendor's.
            </p>
          )}
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

function StocksAndEtfs({
  stocks,
  loading,
  error,
  onViewChain,
}: {
  stocks: StockQuote[]
  loading: boolean
  error: unknown
  onViewChain: (symbol: string) => void
}) {
  const [sort, setSort] = useState<StockSort>(STOCK_RANK_SORT.active)
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const matched = searchStocks(stocks, search)
  const rows = sortStocks(matched, sort)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)
  const rank = stockRankFor(sort)
  const query = search.trim()
  // The most recent session anyone on this table printed in, from the
  // response rather than from the browser's clock.
  const latestDate = latestVolumeDate(stocks)

  return (
    <section
      aria-labelledby="stocks-etfs-heading"
      className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
        <div>
          <h2 id="stocks-etfs-heading" className="text-title-lg text-on-surface">
            Stocks &amp; ETFs
          </h2>
          <p className="mt-1 text-caption text-on-surface-variant">
            {query === ''
              ? `${formatInteger(rows.length)} symbols`
              : `${formatInteger(rows.length)} of ${formatInteger(stocks.length)} symbols`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setExpanded(null)
              setPage(1)
            }}
            aria-label="Search stocks by symbol or name"
            placeholder="Search symbol or name…"
            className={SEARCH}
          />
          <select
            value={rank ?? 'custom'}
            onChange={(e) => {
              setSort(STOCK_RANK_SORT[e.target.value as StockRank])
              setPage(1)
            }}
            aria-label="Rank stocks by"
            className={SELECT}
          >
            {rank === null && <option value="custom">Custom sort</option>}
            {STOCK_RANKS.map((r) => (
              <option key={r} value={r}>
                {STOCK_RANK_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error !== null && error !== undefined ? (
        <div className="px-4 py-6">
          <RequestFailed error={error} what="the stock universe" />
        </div>
      ) : loading ? (
        <TableSkeleton rows={8} columns={STOCK_COLUMNS.length} label="Loading stocks and ETFs" />
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 max-w-prose text-body-md text-on-surface-variant">
          {query === ''
            ? 'The server quoted no symbols at all. That is a data-source answer rather than an empty universe — check the Settings page for the state of the market-data feed.'
            : `Nothing in the universe matches “${query}”. The search covers ticker and company name, so “reddit” finds RDDT — clear it to see all ${formatInteger(stocks.length)} symbols.`}
        </p>
      ) : (
        <>
          <table className="w-full border-collapse">
            <SortableHead
              columns={STOCK_COLUMNS}
              sort={sort}
              onSort={(key) => {
                setSort(nextSort(sort, key, CHAIN_DEFAULT_DIRECTION))
                setPage(1)
              }}
            />
            <tbody>
              {pageItems.map((s) => {
                const rel = relativeVolume(s)
                const open = expanded === s.symbol
                const basis = volumeBasis(s, latestDate)
                return (
                  <Fragment key={s.symbol}>
                    <tr
                      className={`h-8 border-t border-outline/10 ${
                        open ? 'bg-surface-container-low' : 'hover:bg-surface-container-low'
                      }`}
                    >
                      <td className={`${TD} text-body-sm text-on-surface`}>{s.symbol}</td>
                      <td
                        className={`${TD} max-w-0 text-body-sm text-on-surface-variant`}
                        title={s.name}
                      >
                        <span className="block truncate">{s.name}</span>
                      </td>
                      <td className={TD_NUM}>{formatUsd(s.price)}</td>
                      <SignedCell value={s.change} reason={NO_PREVIOUS_CLOSE} />
                      <SignedCell value={s.changePct} percent reason={NO_PREVIOUS_CLOSE} />
                      {/* Compact, unlike the chain's volume: nine digits of
                          share count would set the column's width for the
                          sake of precision nobody reads off a screener.

                          The session is named beside the number, per row.
                          "Traded so far today" and "all of last Monday" are
                          different measurements, and a column that means
                          either one silently makes a busy stock read as
                          quiet. A completed session that is not the latest
                          one on the table shows its date instead, so a
                          symbol that stopped printing is visibly stale. */}
                      <td className={`${TD_NUM}`} title={volumeTitle(basis, s.volumeDate)}>
                        {s.volume === null ? (
                          <Unavailable reason="no daily bar in the window; this is not a zero" />
                        ) : (
                          <>
                            {formatCompactNumber(s.volume)}
                            <span
                              className={`ml-1 text-caption ${
                                basis === 'stale' ? 'text-caution' : 'text-on-surface-variant'
                              }`}
                            >
                              {volumeNote(basis, s.volumeDate)}
                            </span>
                          </>
                        )}
                      </td>
                      {/* Today against the name's own average — what
                          "trending" actually measures. Deliberately not
                          bullish or bearish: unusual volume carries no
                          direction, and a green 4.1× beside a stock down 6%
                          would say the opposite of what happened.

                          `on-accent-container`, not `accent`. Bare accent is
                          #ce8f82 and measures 2.53:1 on surface — the same
                          muted-plausible-colour trap CLAUDE.md documents for
                          `outline` and `neutral`. DESIGN.md only ever uses
                          accent as a *fill* under `on-accent`; as text it is
                          half the required floor. The container's on-colour
                          is 10.7:1 light and 14.3:1 dark. */}
                      <td
                        className={`${TD} whitespace-nowrap text-right text-data-md ${
                          rel !== null && rel >= 2
                            ? 'font-semibold text-on-accent-container'
                            : 'text-on-surface'
                        }`}
                        title={
                          rel === null
                            ? 'Relative volume needs both a session volume and an average; one of them is missing'
                            : `${formatCompactNumber(s.volume ?? 0)} against a ${formatCompactNumber(
                                s.avgVolume ?? 0,
                              )} average`
                        }
                      >
                        {rel === null ? (
                          <Unavailable reason="no session volume or no average to divide by" />
                        ) : (
                          `${rel.toFixed(2)}×`
                        )}
                      </td>
                      {/* A fund has no market cap. formatMarketCap renders
                          the em dash, and sortStocks sorts those rows last
                          rather than treating them as zero. */}
                      <td
                        className={`${TD} whitespace-nowrap text-right text-data-md ${
                          s.marketCap === null ? 'text-on-surface-variant' : 'text-on-surface'
                        }`}
                        title={s.marketCap === null ? 'A fund has no market capitalisation' : ''}
                      >
                        {formatMarketCap(s.marketCap)}
                      </td>
                      <td className={`${TD} whitespace-nowrap text-right`}>
                        <button
                          type="button"
                          aria-expanded={open}
                          aria-label={`${open ? 'Hide' : 'Show'} ${s.symbol} chart`}
                          onClick={() => setExpanded(open ? null : s.symbol)}
                          className={ROW_BUTTON}
                        >
                          {open ? 'Hide' : 'Chart'}
                        </button>
                      </td>
                    </tr>
                    {open && (
                      <tr>
                        <td colSpan={STOCK_COLUMNS.length} className="p-0">
                          <div className="border-t border-outline-warm bg-surface-container-low px-4 py-4">
                            <UnderlyingChart symbol={s.symbol} />
                            {/* Offered on every quoted name. Phase 1 could
                                tell you in advance which had a listed chain,
                                because the fixture was the whole universe;
                                live, that question costs a request per
                                symbol, so the chain's own empty state
                                answers it instead of a button that lies
                                either way. */}
                            <button
                              type="button"
                              onClick={() => onViewChain(s.symbol)}
                              className="mt-3 rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container"
                            >
                              View {s.symbol} chain →
                            </button>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

export function Markets() {
  const accountMode = useUIStore((s) => s.accountMode)

  const stocksQuery = useStocks()
  // Equity, for the ticket's advisory risk estimate only. The engine
  // enforces the limit; this number informs.
  const accountQuery = useAccount()

  const stocks = stocksQuery.data ?? []
  const symbols = underlyingSymbols(stocks)

  // Nothing is chosen until you choose it: `useChain(null)` stays disabled,
  // which is the point of the null.
  const [underlying, setUnderlying] = useState<string | null>(null)
  const spot = stocks.find((s) => s.symbol === underlying)?.price ?? null

  // Not an error, and not a reason to blank the page: prices and chains are
  // the same for both books. Only the ticket's percentage-of-equity needs
  // the account, so only that says it cannot be computed.
  const cashUnavailable = isAccountUnavailable(accountQuery.error)
  const equity = accountQuery.data?.equity ?? null

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-display-lg text-on-surface">Markets</h1>
        <div className="flex items-center gap-2">
          <span className="whitespace-nowrap text-caption text-on-surface-variant">
            {stocksQuery.dataUpdatedAt ? (
              <>
                Read{' '}
                <span className="text-data-md">
                  {formatTimeET(new Date(stocksQuery.dataUpdatedAt).toISOString())}
                </span>{' '}
                ET
              </>
            ) : (
              'Reading the universe…'
            )}
          </span>
          <RefreshButton
            label="Refresh market snapshots"
            // Re-reading a snapshot costs one request, not a scan.
            cooldownMs={0}
            onRefresh={() => {
              void stocksQuery.refetch()
              void accountQuery.refetch()
            }}
          />
        </div>
      </div>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Listed option chains and the stock universe the scanner draws from. These are snapshot
        reads, taken when you open the page and when you refresh — every option contract is its own
        symbol and the socket’s 30-symbol budget belongs to open positions, so nothing here streams.
        Trading a row opens a position in your {accountMode === 'paper' ? 'Paper' : 'Cash'} account.
      </p>

      {cashUnavailable && (
        <p
          role="status"
          className="mt-4 max-w-prose rounded-lg border border-caution bg-caution-container px-4 py-3 text-body-md text-on-caution-container"
        >
          {isApiError(accountQuery.error)
            ? accountQuery.error.message
            : 'Cash is selected and is not configured.'}{' '}
          Prices and chains below are unaffected — they are the same for both books — but an
          order ticket cannot state a percentage of an account balance it could not read.
        </p>
      )}

      <OptionsChains
        underlying={underlying}
        onUnderlyingChange={setUnderlying}
        symbols={symbols}
        spot={spot}
        equity={equity}
        equityUnavailable={cashUnavailable || accountQuery.isError}
      />
      <StocksAndEtfs
        stocks={stocks}
        loading={stocksQuery.isPending}
        error={stocksQuery.error}
        onViewChain={(symbol) => {
          setUnderlying(symbol)
          // Scrolling is the point: the chain is a section above, and
          // pointing it at a symbol without going there would look like the
          // button did nothing. Optional-called because jsdom has no
          // implementation and this is not worth a test double.
          document.getElementById(CHAIN_SECTION_ID)?.scrollIntoView?.({
            behavior: 'smooth',
            block: 'start',
          })
        }}
      />
    </div>
  )
}
