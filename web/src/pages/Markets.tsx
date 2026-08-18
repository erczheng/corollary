import { useState } from 'react'
import { Pagination } from '../components/Pagination'
import { usePagination } from '../hooks/usePagination'
import { OPTION_CHAIN, STOCKS } from '../lib/mockData'
import {
  CHAIN_RANKS,
  CHAIN_RANK_COLUMN,
  CHAIN_RANK_DIRECTION,
  CHAIN_RANK_LABEL,
  CHAIN_UNDERLYINGS,
  MIN_VOLUME_STEPS,
  STOCK_RANKS,
  STOCK_RANK_COLUMN,
  STOCK_RANK_DIRECTION,
  STOCK_RANK_LABEL,
  filterChain,
  rankChain,
  rankStocks,
  type ChainRank,
  type StockRank,
} from '../lib/markets'
import {
  formatCompactNumber,
  formatDateOnly,
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

const TH = 'whitespace-nowrap px-3 py-2 text-label-md uppercase text-on-surface-variant'
const TD = 'px-3 py-2 align-middle'
const TD_NUM = `${TD} whitespace-nowrap text-right text-data-md text-on-surface`

const SELECT =
  'rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface focus:border-primary'

interface Column {
  key: string
  label: string
  align: 'left' | 'right'
  /** Absorbs the table's slack so the neighbouring columns stay tight to
   * their content. At most one per table, and only where a column is
   * genuinely the one that should take the width — a stock's Name. The
   * chain has none: twelve content-shaped columns with one pinned to
   * `w-full` put the table's entire slack into a single gap between Type
   * and Strike. */
  grow?: boolean
}

/** A header that names the column the current view is sorted by. Without
 * it, "Top gainers" and "Highest IV" produce two shuffled tables with no
 * indication of what either is ordered on. `aria-sort` carries the same
 * fact to a screen reader, which gets nothing from the caret. */
function TableHead({
  columns,
  sortedBy,
  direction,
}: {
  columns: Column[]
  sortedBy: string
  direction: 'ascending' | 'descending'
}) {
  return (
    <thead>
      <tr className="bg-surface-container">
        {columns.map((c) => {
          const sorted = c.key === sortedBy
          return (
            <th
              key={c.key}
              aria-sort={sorted ? direction : undefined}
              className={`${TH} ${c.align === 'right' ? 'text-right' : 'text-left'} ${
                c.grow ? 'w-full' : ''
              } ${sorted ? 'text-on-surface' : ''}`}
            >
              {c.label}
              {sorted && (
                <span aria-hidden="true" className="ml-1">
                  {direction === 'ascending' ? '▴' : '▾'}
                </span>
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

const CHAIN_COLUMNS: Column[] = [
  { key: 'symbol', label: 'Symbol', align: 'left' },
  { key: 'expiration', label: 'Exp', align: 'left' },
  { key: 'type', label: 'Type', align: 'left' },
  { key: 'strike', label: 'Strike', align: 'right' },
  { key: 'last', label: 'Last', align: 'right' },
  { key: 'change', label: 'Change', align: 'right' },
  { key: 'changePct', label: 'Change %', align: 'right' },
  { key: 'bid', label: 'Bid', align: 'right' },
  { key: 'ask', label: 'Ask', align: 'right' },
  { key: 'volume', label: 'Volume', align: 'right' },
  { key: 'openInterest', label: 'OI', align: 'right' },
  { key: 'iv', label: 'IV', align: 'right' },
]

const STOCK_COLUMNS: Column[] = [
  { key: 'symbol', label: 'Symbol', align: 'left' },
  { key: 'name', label: 'Name', align: 'left', grow: true },
  { key: 'price', label: 'Price', align: 'right' },
  { key: 'change', label: 'Change', align: 'right' },
  { key: 'changePct', label: 'Change %', align: 'right' },
  { key: 'volume', label: 'Volume', align: 'right' },
  { key: 'marketCap', label: 'Market cap', align: 'right' },
  { key: 'listedOn', label: 'Listed', align: 'right' },
]

function OptionsChains() {
  // A chain is browsed one underlying at a time — that is the question you
  // arrive with, so the page opens on one rather than on 180 rows of six
  // names interleaved. "All underlyings" exists for the screens below,
  // where ranking across the whole board is the point.
  const [underlying, setUnderlying] = useState<string | null>(CHAIN_UNDERLYINGS[0] ?? null)
  const [minVolume, setMinVolume] = useState(0)
  const [rank, setRank] = useState<ChainRank>('strike')

  const rows = rankChain(filterChain(OPTION_CHAIN, { underlying, minVolume }), rank)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)

  return (
    <section
      aria-labelledby="options-chains-heading"
      className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
        <div>
          <h2 id="options-chains-heading" className="text-title-lg text-on-surface">
            Options chains
          </h2>
          <p className="mt-1 text-caption text-on-surface-variant">
            {formatInteger(rows.length)} of {formatInteger(OPTION_CHAIN.length)} contracts
            {underlying === null ? '' : ` in ${underlying}`}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={underlying ?? ''}
            onChange={(e) => {
              setUnderlying(e.target.value === '' ? null : e.target.value)
              setPage(1)
            }}
            aria-label="Filter chain by underlying"
            className={SELECT}
          >
            <option value="">All underlyings</option>
            {CHAIN_UNDERLYINGS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <select
            value={minVolume}
            onChange={(e) => {
              setMinVolume(Number(e.target.value))
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
            value={rank}
            onChange={(e) => {
              setRank(e.target.value as ChainRank)
              setPage(1)
            }}
            aria-label="Rank chain by"
            className={SELECT}
          >
            {CHAIN_RANKS.map((r) => (
              <option key={r} value={r}>
                {CHAIN_RANK_LABEL[r]}
              </option>
            ))}
          </select>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="px-4 py-6 text-body-md text-on-surface-variant">
          Nothing in {underlying ?? 'the listed universe'} trades {formatInteger(minVolume)} contracts
          or more today. That is an answer about a thin chain rather than an empty screen — lower the
          volume floor to see what is listed.
        </p>
      ) : (
        <>
          <table className="w-full border-collapse">
            <TableHead
              columns={CHAIN_COLUMNS}
              sortedBy={CHAIN_RANK_COLUMN[rank]}
              direction={CHAIN_RANK_DIRECTION[rank]}
            />
            <tbody>
              {pageItems.map((c) => (
                <tr
                  key={`${c.symbol}-${c.expiration}-${c.strike}-${c.type}`}
                  className="border-t border-outline/10 hover:bg-surface-container-low"
                >
                  <td className={`${TD} text-body-md text-on-surface`}>{c.symbol}</td>
                  {/* An expiry is a date, not an instant — formatExpiry
                      parses it as UTC. Rendered in ET it would show the
                      day before. */}
                  <td className={`${TD} whitespace-nowrap text-body-md text-on-surface-variant`}>
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
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

function StocksAndEtfs() {
  const [rank, setRank] = useState<StockRank>('active')

  const rows = rankStocks(STOCKS, rank)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)

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
            {formatInteger(rows.length)} symbols
          </p>
        </div>
        <select
          value={rank}
          onChange={(e) => {
            setRank(e.target.value as StockRank)
            setPage(1)
          }}
          aria-label="Rank stocks by"
          className={SELECT}
        >
          {STOCK_RANKS.map((r) => (
            <option key={r} value={r}>
              {STOCK_RANK_LABEL[r]}
            </option>
          ))}
        </select>
      </div>

      {rows.length === 0 ? (
        <p className="px-4 py-6 text-body-md text-on-surface-variant">
          No symbols in the universe yet. Phase 2 fills this from the asset list the scanner runs
          over.
        </p>
      ) : (
        <>
          <table className="w-full border-collapse">
            <TableHead
              columns={STOCK_COLUMNS}
              sortedBy={STOCK_RANK_COLUMN[rank]}
              direction={STOCK_RANK_DIRECTION[rank]}
            />
            <tbody>
              {pageItems.map((s) => (
                <tr
                  key={s.symbol}
                  className="border-t border-outline/10 hover:bg-surface-container-low"
                >
                  <td className={`${TD} text-body-md text-on-surface`}>{s.symbol}</td>
                  <td className={`${TD} max-w-0 text-body-md text-on-surface-variant`} title={s.name}>
                    <span className="block truncate">{s.name}</span>
                  </td>
                  <td className={TD_NUM}>{formatUsd(s.price)}</td>
                  <SignedCell value={s.change} />
                  <SignedCell value={s.changePct} percent />
                  {/* Compact, unlike the chain's volume: nine digits of
                      share count would set the column's width for the sake
                      of precision nobody reads off a screener. */}
                  <td className={TD_NUM}>{formatCompactNumber(s.volume)}</td>
                  {/* A fund has no market cap. formatMarketCap renders the
                      em dash, and rankStocks sorts those rows last rather
                      than treating them as zero. */}
                  <td
                    className={`${TD} whitespace-nowrap text-right text-data-md ${
                      s.marketCap === null ? 'text-on-surface-variant' : 'text-on-surface'
                    }`}
                  >
                    {formatMarketCap(s.marketCap)}
                  </td>
                  <td
                    className={`${TD} whitespace-nowrap text-right text-caption text-on-surface-variant`}
                  >
                    {formatDateOnly(s.listedOn)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

export function Markets() {
  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <h1 className="text-display-lg text-on-surface">Markets</h1>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Listed option chains and the stock universe the scanner draws from. Nothing here streams,
        unlike Activity — every contract is its own symbol and the live stream's 30-symbol budget
        belongs to open positions, so chains run on polled snapshots. Phase 1 renders fixtures.
      </p>

      <OptionsChains />
      <StocksAndEtfs />
    </div>
  )
}
