import { useState } from 'react'
import { StatCard } from '../components/StatCard'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { Pagination } from '../components/Pagination'
import { PositionRow } from '../components/PositionRow'
import { RefreshButton } from '../components/RefreshButton'
import { RequestFailed } from '../components/RequestFailed'
import { WorkingOrders } from '../components/WorkingOrders'
import { StatCardSkeleton, TableSkeleton } from '../components/Skeleton'
import { BankIcon, TrendingDownIcon, TrendingUpIcon } from '../components/icons'
import {
  ExecutionsTable,
  StatusFilterSelect,
  activityCsvRows,
  type ActivityFilter,
} from '../components/ExecutionsTable'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import {
  useAccount,
  useActivity,
  useActivityStats,
  usePositions,
  useWorkingOrders,
} from '../lib/queries'
import { fetchActivity, isAccountUnavailable, isApiError, type ActivityQuery } from '../lib/api'
import { useUIStore } from '../lib/store'
import { downloadCsv } from '../lib/csv'
import { ACCOUNT_LABEL } from '../lib/types'
import type { AccountMode, ActivityItem, Page } from '../lib/types'
import type { TicketMode } from '../lib/orders'
import { formatPct, formatTimeET, formatUsd, signClass } from '../lib/format'

/** Deep enough that pagination is doing real work, short enough that the
 * whole page fits without the table becoming its own scroll region.
 *
 * This is a *server* page size now — it goes out as `pageSize` and the
 * server slices. It matches the API's own default, so a request that
 * omitted it would come back the same length. */
const PAGE_SIZE = 15

/** Position, DTE, Last, Cost basis, Value, Qty, Unrealized P&L, Actions.
 * The expanded panel spans all of them, so this has to stay in step with
 * the header below or the panel will be narrower than the table. */
const POSITION_COLUMNS = 8

/** How long the search box waits before it becomes a request. Long enough
 * that a typed symbol goes out once, short enough not to feel laggy. */
const SEARCH_DEBOUNCE_MS = 250

/** Phase 2 is read-only: no order reaches a broker.
 *
 * Said in the ticket, in words, rather than by hiding the control. The
 * positions on this page are the broker's now, so the Phase 1 store action
 * behind Close/Add/Attach would write to a fixture book nothing renders —
 * a confirm dialog on a real position that silently did nothing, which is
 * worse than an absent button and much worse than a stated absence. */
const READ_ONLY_REASON =
  'Corollary is read-only in this phase — no order reaches the broker. The bid, ask and ' +
  'estimate below are live; placing the order is not wired up yet.'

/** Why the Cancel control on a working order is disabled. Stated for the
 * same reason, and because a cancel would be the first broker write in this
 * codebase and would land before the risk manager exists. */
const CANCEL_UNAVAILABLE_REASON =
  'Cancelling is not wired up in this phase. It would be the first order this codebase sends a ' +
  'broker, and every order has to go through the risk manager, which does not exist yet. Cancel ' +
  'at Alpaca in the meantime.'

/** Whether the regular session has opened, in market time.
 *
 * The client has no market calendar — half-days and holidays are real and
 * this cannot see them — so the sentence it chooses is worded to survive
 * being wrong about the day. The distinction is still worth drawing: an
 * empty feed before the open means the day has not started, and the same
 * empty feed at 3pm means the day produced nothing. Those are different
 * facts and a single sentence covering both says neither. */
function beforeTheOpen(now: Date): boolean {
  const [hour, minute] = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'America/New_York',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
    .format(now)
    .split(':')
    .map(Number)
  return hour * 60 + minute < 9 * 60 + 30
}

/** The 409 the server answers a cash request with while the live keys are
 * absent, rendered as the page rather than as a toast.
 *
 * An empty ledger here would say this account has never traded, which is
 * not what the server said — it said it cannot see the book at all. Those
 * are opposite claims and only one of them is true.
 *
 * `caution`, not `error`: nothing failed, the account is not configured.
 *
 * The variable **names** come from the paper response, which answers on
 * every account. Names only, never values: rule 6 says the UI shows masked
 * presence, and a name is not a mask of anything. */
function CashNotConfigured({ reason, missing }: { reason: string; missing: readonly string[] }) {
  return (
    <section
      aria-labelledby="cash-unavailable-heading"
      className="mt-8 rounded-lg border border-caution bg-caution-container p-6"
    >
      <h2 id="cash-unavailable-heading" className="text-headline-md text-on-caution-container">
        Cash is not configured
      </h2>
      <p className="mt-3 max-w-prose text-body-md text-on-caution-container">{reason}</p>
      {missing.length > 0 && (
        <p className="mt-3 max-w-prose text-body-md text-on-caution-container">
          Missing from the environment: <span className="text-data-md">{missing.join(', ')}</span>.
          Set them in <span className="text-data-md">.env</span> and restart the engine. Corollary
          reads the values; it never displays them.
        </p>
      )}
      <p className="mt-3 max-w-prose text-caption text-on-caution-container">
        No positions, no working orders and no ledger are shown because there are none to read —
        not because the account is empty. Paper is still readable; switch back above.
      </p>
    </section>
  )
}

/** PRD.md §8.2 — open positions, the executions ledger, and rejected orders,
 * for one account.
 *
 * **Everything on this page is server state now.** The header cards come
 * from `GET /api/activity/stats`, which folds *every* realized trade rather
 * than the page on screen (spec decision 11) — they are deliberately not
 * re-derived from `items`, because a lifetime figure computed over a window
 * means something narrower than the word. The table comes from
 * `GET /api/activity`, one page at a time, with `search` and `status`
 * applied by the server and combined there with AND.
 *
 * **Nothing is filtered on this side.** Filtering a page the server already
 * sliced would filter fifteen rows and present the result as the ledger.
 *
 * **This page is polled, not streamed.** It used to be "live" through
 * `store.tick()`, which was the Phase 1 mock broker: it invented prices,
 * filled working orders and triggered attached exits. Real streaming is the
 * WS fan-out at step 8. Until then a "Live" pill over refetched data would
 * claim more than is true, so the panel states the time the figures were
 * read and offers a refresh, the same as Account.
 */
export function Activity() {
  const accountMode = useUIStore((s) => s.accountMode)

  const [filter, setFilter] = useState<ActivityFilter>('all')
  const [search, setSearch] = useState('')
  /** The page number **and the question it belongs to**, held together.
   *
   * Zero-based, which is the API's convention and not `usePagination`'s:
   * that hook is one-based and slices an array already in hand, where this
   * number goes out on the wire and the server does the slicing. The
   * `Pagination` control below is one-based for display, so it is handed
   * `page + 1` and hands back a number with 1 taken off it. Wiring the two
   * together directly would silently skip the first page.
   *
   * **Why the question is stored beside it rather than an effect resetting
   * it.** Changing the search or the filter has to return to page one —
   * otherwise narrowing while deep in the feed asks the server for page 4
   * of a two-page result and gets nothing, which reads as "no results". Done
   * in an effect, that reset lands a render *late*, and the render in
   * between fires a real request for the new question at the old page.
   * Derived, the page is already 0 in the same render the question changes
   * in, and that request never exists. */
  const [paging, setPaging] = useState({ question: '', page: 0 })
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [ticketMode, setTicketMode] = useState<TicketMode>('close')

  const debouncedSearch = useDebouncedValue(search.trim(), SEARCH_DEBOUNCE_MS)

  const question = [debouncedSearch, filter, accountMode].join(' ')
  const page = paging.question === question ? paging.page : 0
  const setPage = (next: number) => setPaging({ question, page: next })

  const query: ActivityQuery = {
    page,
    pageSize: PAGE_SIZE,
    search: debouncedSearch === '' ? undefined : debouncedSearch,
    status: filter === 'all' ? undefined : filter,
  }

  const activityQuery = useActivity(query)
  const statsQuery = useActivityStats()
  const positionsQuery = usePositions()
  const workingQuery = useWorkingOrders()
  // Equity, for the ticket's advisory risk estimate only. The engine
  // enforces the limit; this number informs.
  const accountQuery = useAccount()
  // Paper answers on every account, and its response is what carries the
  // reason Cash is unreadable and the names of the variables that are
  // missing. When Paper is selected this is the same query key as
  // `accountQuery` — one request, not two.
  const paperQuery = useAccount('paper')

  const accountLabel = ACCOUNT_LABEL[accountMode]
  const stats = statsQuery.data
  const ledger = activityQuery.data
  const positions = positionsQuery.data ?? []
  const equity = accountQuery.data?.equity ?? 0

  const header = (
    <>
      {/* The switch sits on the title line because everything below it is
          account-scoped — the positions, the feed, and all three stats. On
          the Dashboard the same control governs the balance and the chart;
          here it governs the entire page. */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-display-lg text-on-surface">Activity</h1>
        <AccountModeToggle />
      </div>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Open positions, executions, and rejected orders for your {accountLabel} account. Alpaca is
        the source of truth for every figure here.
      </p>
    </>
  )

  // Not an error: the server declined to serve one book under the other's
  // name, and said why. Rendered as the page, because there is no row on it
  // that could honestly be filled in.
  if (isAccountUnavailable(activityQuery.error) || isAccountUnavailable(accountQuery.error)) {
    const stated = isApiError(activityQuery.error)
      ? activityQuery.error.message
      : isApiError(accountQuery.error)
        ? accountQuery.error.message
        : ''
    return (
      <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
        {header}
        <CashNotConfigured
          reason={paperQuery.data?.cashAccountUnavailableReason ?? stated}
          missing={paperQuery.data?.missingLiveCredentialEnvVars ?? []}
        />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      {header}

      {/* Three cards in a row, the same treatment the Dashboard gives its
          header stats — these are peer figures, not one composite reading.

          Average win and average loss are bullish/bearish because that is
          what they are. Lifetime P&L takes signClass so a flat account
          reads neutral rather than green. None of the three is ever
          `error` — a losing account is not a broken one. */}
      <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
        {statsQuery.isPending || !stats ? (
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
                    <span className="ml-2 text-body-md">
                      {formatPct(stats.avgWinPct, { signed: true })}
                    </span>
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
                    <span className="ml-2 text-body-md">
                      {formatPct(stats.avgLossPct, { signed: true })}
                    </span>
                  </>
                )
              }
              valueClassName={stats.avgLoss === null ? 'text-on-surface-variant' : 'text-bearish'}
              note={
                stats.losses === 1 ? 'over 1 losing trade' : `over ${stats.losses} losing trades`
              }
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

      {statsQuery.isError && (
        <div className="mt-4">
          <RequestFailed error={statsQuery.error} what="the lifetime figures" />
        </div>
      )}

      {/* The gap in the three figures above, stated beside them rather than
          absorbed into them. `caution`: the numbers are incomplete, which is
          not the same as wrong and not the same as a failure.

          **It names the count and the contracts and stops there.** Why each
          closing went unbooked is not stored by any Phase 2 table (spec
          decision 14 — it lands at step 8), and a confident wrong reason on
          a money figure is worse than an admitted gap. */}
      {stats && stats.notBooked > 0 && (
        <section
          aria-labelledby="not-booked-heading"
          className="mt-4 rounded-lg border border-caution bg-caution-container p-4"
        >
          <h2 id="not-booked-heading" className="text-label-md uppercase text-on-caution-container">
            {stats.notBooked === 1
              ? '1 closing is missing from the figures above'
              : `${stats.notBooked} closings are missing from the figures above`}
          </h2>
          <p className="mt-2 max-w-prose text-body-md text-on-caution-container">
            {stats.notBooked === 1
              ? 'A contract left the book without producing a realized trade, so lifetime P&L and both averages are short by whatever it made or lost.'
              : 'Contracts left the book without producing realized trades, so lifetime P&L and both averages are short by whatever they made or lost.'}{' '}
            The reason is not recorded yet — the count is an arithmetic fact, not a diagnosis.
          </p>
          <p className="mt-2 text-data-md text-on-caution-container">
            {stats.notBookedSymbols.join(', ')}
          </p>
        </section>
      )}

      <section
        aria-labelledby="open-positions-heading"
        className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 id="open-positions-heading" className="text-title-lg text-on-surface">
            Open Positions
          </h2>
          <span className="text-label-md text-on-surface-variant">
            {positionsQuery.isPending
              ? 'Loading…'
              : positionsQuery.isError
                ? ''
                : `${positions.length} open in ${accountLabel}`}
          </span>
        </div>
        {positionsQuery.isPending ? (
          <TableSkeleton rows={3} columns={POSITION_COLUMNS} label="Loading open positions" />
        ) : positionsQuery.isError ? (
          <div className="px-4 py-6">
            <RequestFailed error={positionsQuery.error} what="open positions" />
          </div>
        ) : positions.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            No open positions in this account. New candidates appear on the Dashboard under
            Recommended Trades.
          </p>
        ) : (
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-surface-container">
                <th className="w-full px-3 py-1 text-left text-label-sm uppercase text-on-surface-variant">
                  Position
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  DTE
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  Last
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  Cost basis
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  Value
                </th>
                <th className="px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  Qty
                </th>
                <th className="whitespace-nowrap px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  Unrealized P&L
                </th>
                <th className="px-3 py-1 text-right text-label-sm uppercase text-on-surface-variant">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <PositionRow
                  key={p.id}
                  position={p}
                  columnCount={POSITION_COLUMNS}
                  equity={equity}
                  expanded={expandedId === p.id}
                  mode={expandedId === p.id ? ticketMode : 'close'}
                  submitUnavailableReason={READ_ONLY_REASON}
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

      <WorkingOrders
        orders={workingQuery.data ?? []}
        loading={workingQuery.isPending}
        error={workingQuery.isError ? workingQuery.error : undefined}
        accountLabel={accountLabel}
        cancelUnavailableReason={CANCEL_UNAVAILABLE_REASON}
      />

      <section
        aria-labelledby="recent-activity-heading"
        className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 id="recent-activity-heading" className="text-title-lg text-on-surface">
            Recent Activity
          </h2>
          <div className="flex flex-wrap items-center gap-2">
            {/* What these figures actually are: a ledger read at a moment,
                not a stream. There was a "Live" pill here and it was true
                only of the mock broker that used to drive it. */}
            <span className="whitespace-nowrap text-caption text-on-surface-variant">
              {activityQuery.dataUpdatedAt ? (
                <>
                  Read{' '}
                  <span className="text-data-md">
                    {formatTimeET(new Date(activityQuery.dataUpdatedAt).toISOString())}
                  </span>{' '}
                  ET
                </>
              ) : (
                'Reading the ledger…'
              )}
            </span>
            <RefreshButton
              label="Refresh the ledger"
              // Re-reading your own ledger costs nothing worth throttling.
              cooldownMs={0}
              onRefresh={() => {
                void activityQuery.refetch()
                void statsQuery.refetch()
                void positionsQuery.refetch()
                void workingQuery.refetch()
                void accountQuery.refetch()
              }}
            />
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search activity by symbol or contract"
              placeholder="Search symbol…"
              className="w-44 rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
            />
            <StatusFilterSelect
              value={filter}
              onChange={setFilter}
              label="Filter activity by status"
            />
            <ExportCsvButton accountMode={accountMode} query={query} total={ledger?.total ?? 0} />
          </div>
        </div>
        <Ledger
          isPending={activityQuery.isPending}
          isError={activityQuery.isError}
          error={activityQuery.error}
          page={ledger}
          pageNumber={page}
          onPageChange={setPage}
          search={debouncedSearch}
          filter={filter}
          accountLabel={accountLabel}
        />
      </section>
    </div>
  )
}

/** The ledger itself: skeletons, a designed empty state per reason it is
 * empty, an error in `error` rather than `bearish`, or the table. */
function Ledger({
  isPending,
  isError,
  error,
  page,
  pageNumber,
  onPageChange,
  search,
  filter,
  accountLabel,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  page: Page<ActivityItem> | undefined
  pageNumber: number
  onPageChange: (page: number) => void
  search: string
  filter: ActivityFilter
  accountLabel: string
}) {
  if (isPending) {
    return <TableSkeleton rows={6} columns={7} label="Loading activity" />
  }
  if (isError || !page) {
    return (
      <div className="px-4 py-6">
        <RequestFailed error={error} what="the ledger" />
      </div>
    )
  }

  if (page.total === 0) {
    /* Three different empty states, because they are three different
       facts. A search that found nothing is not an account that has never
       traded, and neither is a filter with no matching rows. */
    return (
      <p className="px-4 py-6 text-body-md text-on-surface-variant">
        {search !== ''
          ? `Nothing matching “${search}”${
              filter === 'all' ? '' : ` with status ${filter}`
            }. The search covers the contract symbol, its label and the action — clear it to see the rest of the ledger.`
          : filter !== 'all'
            ? `No ${filter} activity in the ${accountLabel} account. Other statuses may have rows — clear the filter to see them.`
            : beforeTheOpen(new Date())
              ? `Nothing in the ${accountLabel} ledger, and the market has not opened yet today. This is the whole book rather than today's slice of it, so fills, rejections and cash movements would all be here.`
              : `Nothing in the ${accountLabel} ledger. On a normal trading day the session is under way by now, so this is a book that has not traded rather than one whose day has not started. Fills, rejections and cash movements all land here.`}
      </p>
    )
  }

  const pageCount = Math.max(1, Math.ceil(page.total / PAGE_SIZE))

  return (
    <>
      {/* Rejections spell out the rule that rejected them, inline — this is
          the page of record for them (PRD.md §8.2, CLAUDE.md rule 8), so the
          reason cannot live on hover alone. */}
      <ExecutionsTable items={page.items} layout="full" showRejectionReason />
      {/* One-based for the reader, zero-based on the wire. */}
      <Pagination
        page={pageNumber + 1}
        pageCount={pageCount}
        onChange={(next) => onPageChange(next - 1)}
      />
    </>
  )
}

/** Exports **every row matching the current search and filter**, not the
 * fifteen on screen.
 *
 * A CSV is for reconciling against the broker, so exporting one page of a
 * server-paginated ledger and naming it `activity-paper.csv` would be a file
 * that is quietly wrong about how much you traded. It therefore refetches
 * with the page size opened out rather than reusing the page in hand, and
 * says so if that request fails rather than writing a short file. */
function ExportCsvButton({
  accountMode,
  query,
  total,
}: {
  accountMode: AccountMode
  query: ActivityQuery
  total: number
}) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        disabled={busy || total === 0}
        onClick={() => {
          setBusy(true)
          setFailed(false)
          fetchActivity(accountMode, { ...query, page: 0, pageSize: Math.max(total, 1) })
            .then((full) => {
              downloadCsv(`activity-${accountMode}.csv`, activityCsvRows(full.items))
            })
            .catch(() => setFailed(true))
            .finally(() => setBusy(false))
        }}
        className="rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-40"
      >
        {busy ? 'Exporting…' : 'Export CSV'}
      </button>
      {failed && (
        <span role="alert" className="text-caption text-error">
          The export request failed. Nothing was written.
        </span>
      )}
    </div>
  )
}
