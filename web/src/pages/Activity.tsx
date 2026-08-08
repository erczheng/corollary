import { useState } from 'react'
import { StatCard } from '../components/StatCard'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Pagination } from '../components/Pagination'
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
import { ACCOUNT_LABEL, activityStats, type Position } from '../lib/mockData'
import { formatPct, formatUsd, signClass } from '../lib/format'

/** Deep enough that pagination is doing real work, short enough that the
 * whole page fits without the table becoming its own scroll region. */
const PAGE_SIZE = 15

/** Standard options multiplier. Note the trap CLAUDE.md flags: after a
 * split or special dividend, OCC issues an adjusted root (`AAPL1`) whose
 * deliverable is no longer 100 shares, and this arithmetic is wrong on
 * those. Phase 1 fixtures carry no adjusted contracts; when real positions
 * arrive in Phase 2 the multiplier has to come off the contract, not from
 * here. */
const CONTRACT_MULTIPLIER = 100

/** A long is sold to close at the bid and pays you; a short is bought back
 * at the ask and costs you. Presenting both as "proceeds" would show a
 * debit as though it were a credit — on the one screen where you're
 * deciding whether to take the trade off. */
function closeEstimate(position: Position) {
  const long = position.direction === 'long'
  const price = long ? position.bid : position.ask
  return { long, price, total: price * position.quantity * CONTRACT_MULTIPLIER }
}

export function Activity() {
  const accountMode = useUIStore((s) => s.accountMode)
  const openPositions = useUIStore((s) => s.openPositions[s.accountMode])
  const activity = useUIStore((s) => s.activity[s.accountMode])
  const closePosition = useUIStore((s) => s.closePosition)

  const [filter, setFilter] = useState<ActivityFilter>('all')
  const [closeTarget, setCloseTarget] = useState<Position | null>(null)

  const filtered = filter === 'all' ? activity : activity.filter((a) => a.status === filter)
  const { page, pageCount, pageItems, setPage } = usePagination(filtered, PAGE_SIZE)

  const stats = activityStats(activity)
  const accountLabel = ACCOUNT_LABEL[accountMode]

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
        <AccountModeToggle />
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
      </div>

      <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 className="text-title-lg text-on-surface">Open Positions</h2>
          <span className="text-label-md text-on-surface-variant">
            {openPositions.length} open in {accountLabel}
          </span>
        </div>
        {openPositions.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            No open positions in this account. New candidates appear on the Dashboard under Recommended
            Trades.
          </p>
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-surface-container">
                <th className="w-full px-3 py-2 text-left text-label-md uppercase text-on-surface-variant">
                  Position
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                  Last
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                  Cost basis
                </th>
                <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                  Value
                </th>
                <th className="px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">Qty</th>
                <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                  Unrealized P&L
                </th>
                <th className="px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {openPositions.map((p) => (
                <tr key={p.id} className="border-t border-outline/10 hover:bg-surface-container-low">
                  <td className="max-w-0 px-3 py-2 text-body-md text-on-surface">
                    <span className="block truncate" title={`${p.symbol} ${p.contract}`}>
                      <span className="text-data-md">{p.symbol}</span> {p.contract}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-data-md text-on-surface">{formatUsd(p.last)}</td>
                  <td className="px-3 py-2 text-right text-data-md text-on-surface">
                    {formatUsd(p.costBasis)}
                  </td>
                  <td className="px-3 py-2 text-right text-data-md text-on-surface">{formatUsd(p.value)}</td>
                  <td className="px-3 py-2 text-right text-data-md text-on-surface">{p.quantity}</td>
                  {/* Sign is carried textually as well as by colour — an
                      explicit + or − on both figures (CLAUDE.md). */}
                  <td className={`whitespace-nowrap px-3 py-2 text-right text-data-md ${signClass(p.pnl)}`}>
                    {formatUsd(p.pnl, { signed: true })}
                    <span className="ml-2 text-caption">{formatPct(p.pnlPct, { signed: true })}</span>
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      onClick={() => setCloseTarget(p)}
                      title={`Close ${p.symbol} ${p.contract}`}
                      className="rounded border border-error px-3 py-1 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container"
                    >
                      Close
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 className="text-title-lg text-on-surface">Recent Activity</h2>
          <div className="flex items-center gap-2">
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
        {filtered.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            No {filter === 'all' ? '' : `${filter} `}activity in this account yet.
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

      <ConfirmDialog
        open={closeTarget !== null}
        title="Close this position?"
        consequence={
          closeTarget &&
          (() => {
            const { long, price, total } = closeEstimate(closeTarget)
            return (
              <>
                {long ? 'Sells' : 'Buys back'} {closeTarget.quantity} × {closeTarget.symbol}{' '}
                {closeTarget.contract} at market, crossing the {long ? 'bid' : 'ask'} at{' '}
                {formatUsd(price)}. Current bid {formatUsd(closeTarget.bid)} / ask{' '}
                {formatUsd(closeTarget.ask)}, so estimated {long ? 'proceeds' : 'cost to close'}{' '}
                {formatUsd(total)}. Your other {openPositions.length - 1} position
                {openPositions.length - 1 === 1 ? '' : 's'} and the engine's trading state are unchanged.
              </>
            )
          })()
        }
        confirmLabel="Close position"
        destructive
        onConfirm={() => {
          /* Phase 1 is mock data — nothing is submitted. When this is wired
             up, the close goes through RiskManager.approve() like every
             other order (CLAUDE.md rule 1). Do not call the broker here. */
          if (closeTarget) closePosition(closeTarget.id)
          setCloseTarget(null)
        }}
        onCancel={() => setCloseTarget(null)}
      />
    </div>
  )
}
