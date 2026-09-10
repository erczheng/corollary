import { Fragment, useState } from 'react'
import { Pagination } from '../components/Pagination'
import { LiveStatus } from '../components/LiveStatus'
import { SymbolCombobox } from '../components/SymbolCombobox'
import { ChainOrderTicket } from '../components/ChainOrderTicket'
import { UnderlyingChart } from '../components/UnderlyingChart'
import { TableSkeleton } from '../components/Skeleton'
import { usePagination } from '../hooks/usePagination'
import { useMarketPoll } from '../hooks/useMarketPoll'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS, STOCKS } from '../lib/mockData'
import { type OptionContract } from '../lib/types'
import {
  CHAIN_DEFAULT_DIRECTION,
  CHAIN_LADDER_SORT,
  CHAIN_RANKS,
  CHAIN_RANK_LABEL,
  CHAIN_RANK_SORT,
  CHAIN_UNDERLYINGS,
  MIN_VOLUME_STEPS,
  STOCK_RANKS,
  STOCK_RANK_LABEL,
  STOCK_RANK_SORT,
  chainRankFor,
  filterChain,
  liveStocks,
  relativeVolume,
  searchStocks,
  sortChain,
  sortStocks,
  stockRankFor,
  type ChainRank,
  type ChainSort,
  type ChainSortKey,
  type SortDirection,
  type StockRank,
  type StockSort,
  type StockSortKey,
} from '../lib/markets'
import {
  formatCompactNumber,
  formatExpiry,
  formatInteger,
  formatIv,
  formatMarketCap,
  formatPct,
  formatUsd,
  signClass,
} from '../lib/format'

/** Same depth as Activity's ledger — deep enough that pagination is doing
 * real work, short enough that neither table becomes its own scroll
 * region. */
const PAGE_SIZE = 15

/** How often a market snapshot arrives.
 *
 * Deliberately slower than Activity's 400ms stream, and the gap is
 * architecture rather than preference: the websocket is capped at 30
 * symbols on the Basic plan so it is spent on open positions, while this
 * page polls snapshots against a 200 req/min budget instead. Two seconds
 * across a page of chains sits well inside that; 400ms would not. */
const POLL_MS = 2_000

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

interface Column<K extends string> {
  key: string
  label: string
  align: 'left' | 'right'
  /** Sortable columns are the quote columns and nothing else. Sorting by
   * Type would split a ladder into two blocks that no longer read as a
   * chain, and Strike across three expirations interleaves ladders that do
   * not exist — those are identity, not order. */
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
 * grayscale, in a screenshot, and for a colourblind reader. */
function SignedCell({ value, percent }: { value: number; percent?: boolean }) {
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
  { key: 'openInterest', label: 'OI', align: 'right', sortKey: 'openInterest' },
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

function OptionsChains({
  underlying,
  onUnderlyingChange,
  equity,
  loading,
}: {
  /** Lifted, because the stock table points this at a symbol too. Two
   * copies of it would let the table say AAPL while the chain showed SPY. */
  underlying: string | null
  onUnderlyingChange: (symbol: string | null) => void
  equity: number
  loading: boolean
}) {
  const chain = useUIStore((s) => s.chain)

  const [minVolume, setMinVolume] = useState(0)
  const [sort, setSort] = useState<ChainSort>(CHAIN_LADDER_SORT)
  const [expanded, setExpanded] = useState<string | null>(null)

  const rows = sortChain(filterChain(chain, { underlying, minVolume }), sort)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)

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
            {formatInteger(rows.length)} of {formatInteger(chain.length)} contracts
            {underlying === null ? '' : ` in ${underlying}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <SymbolCombobox
            symbols={CHAIN_UNDERLYINGS}
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

      {loading ? (
        <TableSkeleton rows={8} columns={CHAIN_COLUMNS.length} label="Loading option chains" />
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 text-body-md text-on-surface-variant">
          Nothing in {underlying ?? 'the listed universe'} trades {formatInteger(minVolume)} contracts
          or more today. That is an answer about a thin chain rather than an empty screen — lower the
          volume floor to see what is listed.
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
                        <td className={TD_NUM}>{formatUsd(c.last)}</td>
                        <SignedCell value={c.change} />
                        <SignedCell value={c.changePct} percent />
                        <td className={TD_NUM}>{formatUsd(c.bid)}</td>
                        <td className={TD_NUM}>{formatUsd(c.ask)}</td>
                        <td className={TD_NUM}>{formatInteger(c.volume)}</td>
                        <td className={TD_NUM}>{formatInteger(c.openInterest)}</td>
                        <td className={TD_NUM}>{formatIv(c.iv)}</td>
                        <td className={`${TD} whitespace-nowrap text-right`}>
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
                        </td>
                      </tr>
                      {open && (
                        <tr>
                          <td colSpan={CHAIN_COLUMNS.length} className="p-0">
                            <ChainOrderTicket
                              contract={c}
                              equity={equity}
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
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

function StocksAndEtfs({
  loading,
  onViewChain,
}: {
  loading: boolean
  onViewChain: (symbol: string) => void
}) {
  const quotes = useUIStore((s) => s.underlyings)
  const [sort, setSort] = useState<StockSort>(STOCK_RANK_SORT.active)
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)

  const matched = searchStocks(liveStocks(STOCKS, quotes), search)
  const rows = sortStocks(matched, sort)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)
  const rank = stockRankFor(sort)
  const query = search.trim()

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
              : `${formatInteger(rows.length)} of ${formatInteger(STOCKS.length)} symbols`}
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

      {loading ? (
        <TableSkeleton rows={8} columns={STOCK_COLUMNS.length} label="Loading stocks and ETFs" />
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 text-body-md text-on-surface-variant">
          Nothing in the universe matches “{query}”. The search covers ticker and company name, so
          “reddit” finds RDDT — clear it to see all {formatInteger(STOCKS.length)} symbols.
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
                const hasChain = CHAIN_UNDERLYINGS.includes(s.symbol)
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
                      <SignedCell value={s.change} />
                      <SignedCell value={s.changePct} percent />
                      {/* Compact, unlike the chain's volume: nine digits of
                          share count would set the column's width for the
                          sake of precision nobody reads off a screener. */}
                      <td className={TD_NUM}>{formatCompactNumber(s.volume)}</td>
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
                          rel >= 2 ? 'font-semibold text-on-accent-container' : 'text-on-surface'
                        }`}
                        title={`${formatCompactNumber(s.volume)} today against a ${formatCompactNumber(
                          s.avgVolume,
                        )} average`}
                      >
                        {rel.toFixed(2)}×
                      </td>
                      {/* A fund has no market cap. formatMarketCap renders
                          the em dash, and sortStocks sorts those rows last
                          rather than treating them as zero. */}
                      <td
                        className={`${TD} whitespace-nowrap text-right text-data-md ${
                          s.marketCap === null ? 'text-on-surface-variant' : 'text-on-surface'
                        }`}
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
                            {/* Only where a chain is actually listed. Twenty
                                of these twenty-six names have none, and a
                                button that lands you on an empty chain is
                                worse than no button — this way the control's
                                presence is itself the answer to "can I trade
                                options on this". */}
                            {hasChain ? (
                              <button
                                type="button"
                                onClick={() => onViewChain(s.symbol)}
                                className="mt-3 rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container"
                              >
                                View {s.symbol} chain →
                              </button>
                            ) : (
                              <p className="mt-3 text-caption text-on-surface-variant">
                                No listed chain for {s.symbol} on this provider — it is in the
                                universe the scanner reads, not one you can write a contract on
                                here.
                              </p>
                            )}
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
  const lastPollAt = useUIStore((s) => s.lastPollAt)

  // Held here rather than inside the chain, because the stock table points
  // it at a symbol too.
  const [underlying, setUnderlying] = useState<string | null>(CHAIN_UNDERLYINGS[0] ?? null)

  useMarketPoll(POLL_MS)

  // Loading is a real condition, not a timer: until the first snapshot
  // lands there is nothing current to show. Same rule Activity follows —
  // faking a delay to make the skeletons appear would be theatre.
  const loading = lastPollAt === null

  // Latest balance for this account, for the ticket's advisory risk
  // estimate only. The engine enforces the limit; this number informs.
  const history = ACCOUNT_SNAPSHOTS[accountMode].portfolioHistory
  const equity = history[history.length - 1].value

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-display-lg text-on-surface">Markets</h1>
        <LiveStatus at={lastPollAt} kind="poll" />
      </div>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Listed option chains and the stock universe the scanner draws from. Prices arrive on their
        own here as they do on Activity, but from polled snapshots rather than the stream — every
        contract is its own symbol, and the socket’s 30-symbol budget belongs to open positions.
        Trading a row opens a position in your {accountMode === 'paper' ? 'Paper' : 'Cash'} account.
      </p>

      <OptionsChains
        underlying={underlying}
        onUnderlyingChange={setUnderlying}
        equity={equity}
        loading={loading}
      />
      <StocksAndEtfs
        loading={loading}
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
