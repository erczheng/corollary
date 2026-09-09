import { useState } from 'react'
import { StatCard } from '../components/StatCard'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { Pagination } from '../components/Pagination'
import { PositionRow } from '../components/PositionRow'
import { WorkingOrders } from '../components/WorkingOrders'
import { StatCardSkeleton, TableSkeleton } from '../components/Skeleton'
import { LiveStatus } from '../components/LiveStatus'
import { useLiveTick } from '../hooks/useLiveTick'
import { BankIcon, TrendingDownIcon, TrendingUpIcon } from '../components/icons'
import {
  ExecutionsTable,
  StatusFilterSelect,
  activityCsvRows,
  type ActivityFilter,
} from '../components/ExecutionsTable'
import { usePagination } from '../hooks/usePagination'
import { useUIStore } from '../lib/store'
import { downloadCsv } from '../lib/csv'
import { ACCOUNT_LABEL, ACCOUNT_SNAPSHOTS, activityStats } from '../lib/mockData'
import type { TicketMode } from '../lib/orders'
import { formatPct, formatUsd, signClass } from '../lib/format'

/** Deep enough that pagination is doing real work, short enough that the
 * whole page fits without the table becoming its own scroll region. */
const PAGE_SIZE = 15

/** Position, DTE, Last, Cost basis, Value, Qty, Unrealized P&L, Actions.
 * The expanded panel spans all of them, so this has to stay in step with
 * the header below or the panel will be narrower than the table. */
const POSITION_COLUMNS = 8

/** How often a price arrives. Stands in for the Alpaca WebSocket, which
 * Phase 2 puts in its place.
 *
 * There was a Refresh button here and it was the wrong affordance. The
 * question you have on a positions screen is "are these numbers current",
 * and prices arriving on their own answer it continuously where a button
 * answers it once, for the instant after you press it.
 *
 * Safe to change. The store states volatility per second and scales it by
 * the interval, so this controls how *often* prices move and not how far
 * they travel. Below ~250ms the P&L column starts to be genuinely hard to
 * read, which is a legibility limit rather than a technical one. */
const TICK_MS = 400

export function Activity() {
  const accountMode = useUIStore((s) => s.accountMode)
  const openPositions = useUIStore((s) => s.openPositions[s.accountMode])
  const activity = useUIStore((s) => s.activity[s.accountMode])

  const [filter, setFilter] = useState<ActivityFilter>('all')
  const [search, setSearch] = useState('')
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [ticketMode, setTicketMode] = useState<TicketMode>('close')
  // Prices stream while the page is open. Only the active account ticks —
  // the other book's keys aren't loaded, so nothing is subscribed to it.
  useLiveTick(TICK_MS)

  // Loading is a real condition, not a timer: until the first price
  // arrives there is nothing current to show, which is exactly the state
  // Phase 2 is in while the first snapshot is in flight. Faking a delay
  // to make the skeletons appear would have been theatre.
  const loading = useUIStore((s) => s.lastTickAt) === null

  // Symbol search. Matches the contract text, which carries the symbol —
  // "everything I did in AAPL" is the question a 50-row ledger raises and
  // a status filter cannot answer.
  const query = search.trim().toLowerCase()
  const filtered = activity.filter(
    (a) =>
      (filter === 'all' || a.status === filter) &&
      (query === '' || a.contract.toLowerCase().includes(query) || a.action.toLowerCase().includes(query)),
  )
  const { page, pageCount, pageItems, setPage } = usePagination(filtered, PAGE_SIZE)

  const stats = activityStats(activity)
  const accountLabel = ACCOUNT_LABEL[accountMode]
  // Latest balance for this account, used only for the ticket's advisory
  // risk estimate. The engine enforces the limit; this number informs.
  const history = ACCOUNT_SNAPSHOTS[accountMode].portfolioHistory
  const equity = history[history.length - 1].value

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      {/* The switch sits on the title line because everything below it is
          account-scoped — the positions, the feed, and all three stats. On
          the Dashboard the same control governs the balance and the chart;
          here it governs the entire page, so it belongs at the top of it
          rather than a page away. The header badge still names the account
          for the pages that have no switch of their own. */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-display-lg text-on-surface">Activity</h1>
        <div className="flex items-center gap-3">
          <LiveStatus />
          <AccountModeToggle />
        </div>
      </div>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Open positions, executions, and rejected orders for your {accountLabel} account.
      </p>

      {/* Three cards in a row, the same treatment the Dashboard gives its
          header stats — these are peer figures, not one composite reading,
          and a single panel implied they were.

          Average win and average loss are bullish/bearish because that is
          what they are. Lifetime P&L takes signClass so a flat account
          reads neutral rather than green. None of the three is ever
          `error` — a losing account is not a broken one. */}
      <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
        {loading ? (
          <>
            <StatCardSkeleton label="Loading average win" />
            <StatCardSkeleton label="Loading average loss" />
            <StatCardSkeleton label="Loading lifetime P&L" />
          </>
        ) : (
          <>
        <StatCard
          label="Average win"
          icon={<TrendingUpIcon />}
          value={
            stats.avgWin === null || stats.avgWinPct === null ? (
              '—'
            ) : (
              <>
                {formatUsd(stats.avgWin, { signed: true })}
                <span className="ml-2 text-body-md">{formatPct(stats.avgWinPct, { signed: true })}</span>
              </>
            )
          }
          valueClassName={stats.avgWin === null ? 'text-on-surface-variant' : 'text-bullish'}
          note={stats.wins === 1 ? 'over 1 winning trade' : `over ${stats.wins} winning trades`}
        />
        <StatCard
          label="Average loss"
          icon={<TrendingDownIcon />}
          value={
            stats.avgLoss === null || stats.avgLossPct === null ? (
              '—'
            ) : (
              <>
                {formatUsd(stats.avgLoss, { signed: true })}
                <span className="ml-2 text-body-md">{formatPct(stats.avgLossPct, { signed: true })}</span>
              </>
            )
          }
          valueClassName={stats.avgLoss === null ? 'text-on-surface-variant' : 'text-bearish'}
          note={stats.losses === 1 ? 'over 1 losing trade' : `over ${stats.losses} losing trades`}
        />
        <StatCard
          label="Lifetime P&L"
          icon={<BankIcon />}
          value={formatUsd(stats.lifetimePnl, { signed: true })}
          valueClassName={signClass(stats.lifetimePnl)}
          note="realized only — deposits and withdrawals excluded"
        />
          </>
        )}
      </div>

      <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 className="text-title-lg text-on-surface">Open Positions</h2>
          <span className="text-label-md text-on-surface-variant">
            {loading ? 'Loading…' : `${openPositions.length} open in ${accountLabel}`}
          </span>
        </div>
        {loading ? (
          <TableSkeleton rows={3} columns={POSITION_COLUMNS} label="Loading open positions" />
        ) : openPositions.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            No open positions in this account. New candidates appear on the Dashboard under Recommended
            Trades.
          </p>
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-surface-container">
                <th className="w-full px-3 py-1 text-left text-label-md uppercase text-on-surface-variant">
                  Position
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  DTE
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  Last
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  Cost basis
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  Value
                </th>
                <th className="px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">Qty</th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  Unrealized P&L
                </th>
                <th className="px-3 py-1 text-right text-label-md uppercase text-on-surface-variant">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {openPositions.map((p) => (
                <PositionRow
                  key={p.id}
                  position={p}
                  columnCount={POSITION_COLUMNS}
                  equity={equity}
                  expanded={expandedId === p.id}
                  mode={expandedId === p.id ? ticketMode : 'close'}
                  // One row open at a time. With a handful of positions an
                  // accordion keeps the page short and makes the target of
                  // an action unambiguous.
                  onToggle={() => setExpandedId((id) => (id === p.id ? null : p.id))}
                  onSelectMode={(mode) => {
                    setExpandedId(p.id)
                    setTicketMode(mode)
                  }}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>

      <WorkingOrders loading={loading} accountLabel={accountLabel} />

      <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 className="text-title-lg text-on-surface">Recent Activity</h2>
          <div className="flex items-center gap-2">
            <input
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value)
                setPage(1)
              }}
              aria-label="Search activity by symbol or contract"
              placeholder="Search symbol…"
              className="w-44 rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
            />
            <StatusFilterSelect
              value={filter}
              onChange={(next) => {
                setFilter(next)
                // Back to the first page. Without this, narrowing the filter
                // while deep in the feed lands you on the last page of the
                // new result set, which reads as "no results".
                setPage(1)
              }}
              label="Filter activity by status"
            />
            <button
              type="button"
              onClick={() => downloadCsv(`activity-${accountMode}.csv`, activityCsvRows(filtered))}
              className="rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
            >
              Export CSV
            </button>
          </div>
        </div>
        {loading ? (
          <TableSkeleton rows={6} columns={7} label="Loading activity" />
        ) : filtered.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            {query !== ''
              ? `Nothing matching “${search.trim()}”${filter === 'all' ? '' : ` with status ${filter}`}. Clear the search to see the rest of the ledger.`
              : filter === 'all'
                ? 'No activity in this account yet. Fills, rejections and cash movements all land here.'
                : `No ${filter} activity in this account. Other statuses may have rows — clear the filter to see them.`}
          </p>
        ) : (
          <>
            {/* Rejections spell out the rule that rejected them, inline —
                this is the page of record for them (PRD.md §8.2, CLAUDE.md
                rule 8), so the reason can't live on hover alone. */}
            <ExecutionsTable items={pageItems} layout="full" showRejectionReason />
            <Pagination page={page} pageCount={pageCount} onChange={setPage} />
          </>
        )}
      </section>
    </div>
  )
}
