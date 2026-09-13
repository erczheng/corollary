import { useState } from 'react'
import { Link } from 'react-router-dom'
import { StatCard } from '../components/StatCard'
import { PerformanceChart } from '../components/PerformanceChart'
import { RefreshButton } from '../components/RefreshButton'
import { RequestFailed } from '../components/RequestFailed'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { ExecutionModeToggle } from '../components/ExecutionModeToggle'
import { StatCardSkeleton, TableSkeleton, ValueSkeleton } from '../components/Skeleton'
import {
  ExecutionsTable,
  StatusFilterSelect,
  activityCsvRows,
  type ActivityFilter,
} from '../components/ExecutionsTable'
import {
  BankIcon,
  ChevronDownIcon,
  TargetIcon,
  TrendingDownIcon,
  TrendingUpIcon,
} from '../components/icons'
import { useUIStore } from '../lib/store'
import {
  useAccount,
  useAccountHistory,
  useActivity,
  useActivityStats,
  useEngineState,
  useHaltEngine,
  usePositions,
  useResumeEngine,
} from '../lib/queries'
import { isAccountUnavailable, isApiError } from '../lib/api'
/* A pure predicate, not a fixture — `account.ts` reads it from here too, and
   it is the one thing this page still imports from `mockData`. It belongs in
   `types.ts` beside `ActivityAction`; that file is owned by another dispatch
   this round, so the move is reported rather than made. */
import { isOrderAction } from '../lib/mockData'
import { ACCOUNT_LABEL } from '../lib/types'
import type { ActivityItem, ActivityStats, EquityCurvePoint, PricePoint } from '../lib/types'
import { downloadCsv } from '../lib/csv'
import {
  formatDateTimeET,
  formatStrategyName,
  formatTimeET,
  formatUsd,
  signClass,
} from '../lib/format'

/** How many executions the panel shows. It is the morning page, not the
 * ledger — "Recent Executions" running to fifty rows would make "View all"
 * decorative, and Activity pages the rest. */
const EXECUTIONS_SHOWN = 10

/** How many ledger rows are asked for to fill those ten.
 *
 * The server has no *orders-only* filter — `status` and `search` are the
 * whole of what `/api/activity` takes — so cash movements have to be dropped
 * on this side, and a request for exactly ten would show seven the moment
 * three deposits landed. Over-fetching and slicing to ten keeps the panel's
 * stated depth true. It is deliberately **not** the Activity page's rule
 * (nothing filtered client-side): that page presents a page of the ledger as
 * the ledger, where this one presents a fixed recent window of *orders* and
 * links to the ledger for the rest. */
const EXECUTIONS_FETCHED = 30

/** A year of daily closes, so the chart's own range control has something to
 * slice. Asking for the default 1M and then offering a 1Y button would draw
 * one month under a one-year label.
 *
 * `1A` rather than `1Y`: the server's grammar is a count followed by D, W, M
 * or A, and it rejects anything else with a stated reason. */
const HISTORY_WINDOW = { period: '1A', timeframe: '1D' } as const

/** Spec decision 2 — every write control is inert in this phase, disabled
 * with a one-line reason. Closing a position is an order, every order goes
 * through the risk manager (rule 1), and the risk manager arrives in Phase 6.
 *
 * Stated in words rather than by hiding the button: the positions this page
 * counts are the broker's now, and a Flatten that quietly closed nothing
 * would leave you believing you were flat. */
const FLATTEN_UNAVAILABLE_REASON =
  'Flatten is disabled in this phase. Closing a position is an order, every order has to go ' +
  'through the risk manager, and that arrives in Phase 6 — a Flatten that silently did nothing ' +
  'would leave you believing you were flat. Close at Alpaca in the meantime.'

/** The reason a halt taken from this page records. Rule 8: a state change
 * carries who asked for it, not just that it happened. */
const HALT_REASON = 'Halted by hand from the Dashboard'

/** Alpaca's equity curve as the chart's series.
 *
 * Two kinds of point are dropped, both padding rather than balances:
 *
 * - `equity: null` carries no balance at all.
 * - The **leading run of zeroes**. A one-year window is padded back to its
 *   start whether or not the account existed then — the live paper account
 *   returns 223 zeroes before the day it was funded — and a line climbing
 *   from $0 to $100,000 in one step flattens every real move on the curve
 *   against that scale. Only the *leading* run goes: a zero later in a curve
 *   is a real, catastrophic balance and must stay visible.
 *
 * `at` is an ISO datetime in UTC and `PricePoint.date` is date-only, so the
 * date is taken off the UTC instant rather than through a local `Date` — the
 * same off-by-one trap `formatExpiry` documents.
 *
 * Exported for the test: it is the one piece of arithmetic on this page, and
 * it is worth asserting without rendering Recharts. */
export function equityCurve(points: readonly EquityCurvePoint[]): PricePoint[] {
  const series: PricePoint[] = []
  for (const point of points) {
    if (point.equity === null) continue
    if (series.length === 0 && point.equity === 0) continue
    series.push({ date: point.at.slice(0, 10), value: point.equity })
  }
  return series
}

/** The account's realized win rate, or null where nothing has been realized.
 *
 * `wins` and `losses` are folded server-side over every realized trade (spec
 * decision 11); the ratio between them is display-only arithmetic over two
 * authoritative counts. Zero settled trades is **not** a 0% win rate — it is
 * no win rate at all, and rendering 0% would report a losing record the
 * account does not have. */
export function realizedWinRate(stats: ActivityStats | undefined): number | null {
  if (!stats) return null
  const settled = stats.wins + stats.losses
  return settled === 0 ? null : (stats.wins / settled) * 100
}

/** The oldest figure on screen, in epoch ms, or null while nothing has
 * landed.
 *
 * The *oldest*, not the newest: the line reads "Read …", and quoting the
 * freshest of several independent requests would date the page by whichever
 * one happened to refetch last. Nothing on screen is older than this. */
export function oldestRead(stamps: readonly number[]): number | null {
  const landed = stamps.filter((t) => t > 0)
  return landed.length === 0 ? null : Math.min(...landed)
}

/** The ten most recent *orders* out of the page the server returned. Cash
 * movements are dropped: a deposit has no contract, no quantity and no fill,
 * so it is not an execution, and the full ledger with transfers in it is on
 * Activity (PRD §8.2). */
function executionsShown(items: readonly ActivityItem[] | undefined): ActivityItem[] {
  return (items ?? []).filter((a) => isOrderAction(a.action)).slice(0, EXECUTIONS_SHOWN)
}

/** The 409 the server answers a cash request with while the live keys are
 * absent, rendered as the page.
 *
 * `caution`, not `error`: nothing failed, the account is not configured. An
 * empty dashboard here would read as a real account sitting idle, which is
 * the opposite of what the server said.
 *
 * The variable **names** come from the paper response, which answers on every
 * account. Names only, never values — rule 6. */
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
        No balance, no equity curve and no executions are shown because there are none to read —
        not because the account is empty. Paper is still readable; switch back above.
      </p>
    </section>
  )
}

/** PRD.md §8.1 — balance, the day's performance, the equity curve, today's
 * candidates, and the recent executions, for one account.
 *
 * **Everything with a number on it is server state.** The balance and the day
 * change are the broker's own figures rather than a sum of the rows on
 * screen; the curve is Alpaca's, with Corollary's first run marked on it
 * (spec decision 6); the executions are a window of `GET /api/activity`; the
 * halt is `GET /api/engine/state`.
 *
 * **This page is polled, not streamed.** It was "live" through
 * `store.tick()`, which was the Phase 1 mock broker — it invented prices and
 * filled its own orders. Real streaming is the WS fan-out at step 8, so until
 * then the page states when it was read and offers a refresh, the same as
 * Account and Activity. A "Live" pill over refetched data claims more than is
 * true.
 */
export function Dashboard() {
  const activeStrategyId = useUIStore((s) => s.activeStrategyId)
  const setActiveStrategyId = useUIStore((s) => s.setActiveStrategyId)
  const accountMode = useUIStore((s) => s.accountMode)
  const executionMode = useUIStore((s) => s.executionMode)
  const strategies = useUIStore((s) => s.strategies)

  const [activityFilter, setActivityFilter] = useState<ActivityFilter>('all')

  const accountQuery = useAccount()
  const historyQuery = useAccountHistory(HISTORY_WINDOW)
  const statsQuery = useActivityStats()
  const positionsQuery = usePositions()
  const engineQuery = useEngineState()
  const executionsQuery = useActivity({
    page: 0,
    pageSize: EXECUTIONS_FETCHED,
    status: activityFilter === 'all' ? undefined : activityFilter,
  })
  // Paper answers on every account, and its response carries the reason Cash
  // is unreadable plus the names of the variables that are missing. While
  // Paper is selected this is the same query key as `accountQuery` — one
  // request, not two.
  const paperQuery = useAccount('paper')

  const halt = useHaltEngine()
  const resume = useResumeEngine()

  const accountLabel = ACCOUNT_LABEL[accountMode]
  const account = accountQuery.data
  const engine = engineQuery.data
  const openPositionCount = positionsQuery.data?.length ?? 0

  /* From the store: Research renames, promotes, retires and deletes these,
     and a dropdown reading the fixture would keep offering a strategy that no
     longer exists under a name that has changed. Strategies stay a fixture
     this phase — there is no engine to run one — and the caption under the
     controls says so rather than letting the list read as live. */
  const activeStrategy = strategies.find((s) => s.id === activeStrategyId) ?? strategies[0]

  // Halt is the *engine's* state now, not the UI's. Manual/Auto still decides
  // whether the engine would act unattended, and the pill reports that, but
  // the halt itself comes off the wire — the dead-man's switch can set it
  // while nobody is looking (rule 9).
  const isAuto = executionMode === 'auto'
  const engineHalted = engine?.halted ?? false

  const readAt = oldestRead([
    accountQuery.dataUpdatedAt,
    historyQuery.dataUpdatedAt,
    executionsQuery.dataUpdatedAt,
    engineQuery.dataUpdatedAt,
  ])

  const refreshAll = () => {
    void accountQuery.refetch()
    void historyQuery.refetch()
    void statsQuery.refetch()
    void positionsQuery.refetch()
    void engineQuery.refetch()
    void executionsQuery.refetch()
  }

  const header = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-display-lg text-on-surface">Portfolio Overview</h1>
        {/* The trading-state pill — primary, per DESIGN.md's Colors section.
            Read-only: it reports whether the engine is placing orders on its
            own and which account is live. The controls that change either of
            those are in the row below.

            "Trading On" is reserved for Auto, because that is the only state
            in which the engine acts unattended. In Manual it says Manual.

            While the engine's state is still in flight it says so instead of
            guessing: a pill reading "Trading On" for one render because
            nothing has answered yet is the one wrong answer here.

            The status dot uses the *-container semantics rather than bare
            `bullish`/`caution`/`neutral`, which sit at nearly the same
            lightness as `primary` in both themes and vanished on the pill. */}
        <span className="flex items-center gap-2 whitespace-nowrap rounded-full bg-primary px-4 py-2 text-label-md text-on-primary">
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${
              engineQuery.isPending || !isAuto
                ? 'bg-neutral-container'
                : engineHalted
                  ? 'bg-caution-container'
                  : 'bg-bullish-container'
            }`}
            aria-hidden="true"
          />
          {engineQuery.isPending
            ? 'Reading engine state'
            : !isAuto
              ? 'Manual'
              : engineHalted
                ? 'Halted'
                : 'Trading On'}{' '}
          ({accountMode === 'cash' ? 'Cash' : 'Paper'})
        </span>
      </div>

      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Monitor your total balance, the day's performance, and today's recommended trades. Alpaca is
        the source of truth for every figure here.
      </p>
    </>
  )

  // Not an error: the server declined to serve one book under the other's
  // name, and said why. Rendered as the page, because there is no card on it
  // that could honestly be filled in.
  if (isAccountUnavailable(accountQuery.error) || isAccountUnavailable(historyQuery.error)) {
    const stated = isApiError(accountQuery.error)
      ? accountQuery.error.message
      : isApiError(historyQuery.error)
        ? historyQuery.error.message
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

      {/* What these figures are: a set of reads at a moment, not a stream.
          There was a "Live" pill here and it was true only of the mock broker
          that used to drive it. The time quoted is the *oldest* of the reads,
          so it is a floor rather than a flattering figure. */}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="whitespace-nowrap text-caption text-on-surface-variant">
          {readAt === null ? (
            'Reading the account…'
          ) : (
            <>
              Read{' '}
              <span className="text-data-md">{formatTimeET(new Date(readAt).toISOString())}</span> ET
            </>
          )}
        </span>
        <RefreshButton
          label="Refresh the dashboard"
          // Re-reading your own account costs nothing worth throttling.
          cooldownMs={0}
          onRefresh={refreshAll}
        />
      </div>

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <AccountModeToggle />
        <ExecutionModeToggle />
        <div className="relative shrink-0">
          <select
            value={activeStrategy?.id ?? ''}
            onChange={(e) => setActiveStrategyId(e.target.value)}
            aria-label="Active strategy"
            className="appearance-none whitespace-nowrap rounded-full border border-outline bg-surface-container-low py-2 pl-4 pr-8 text-label-md text-on-surface focus:border-primary"
          >
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>
                Strategy: {formatStrategyName(s.name)}
              </option>
            ))}
          </select>
          <ChevronDownIcon className="pointer-events-none absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-on-surface-variant" />
        </div>

        {/* Actions sit apart from the settings above so Flatten — the one
            destructive control on this page — isn't adjacent to the toggles
            you'd click casually. */}
        <div className="flex items-center gap-3 sm:ml-auto">
          {/* Halt and Resume are **real engine state** in this phase (spec
              decision 2), because rule 9's dead-man's switch fires on a real
              connection. They are no longer gated on Auto: the switch can
              halt the engine while this page sits in Manual, and rule 9's
              explicit human resume must not be reachable only by first
              flipping an unrelated toggle. */}
          {engineQuery.isPending ? null : engineHalted ? (
            <button
              type="button"
              onClick={() => resume.mutate()}
              disabled={resume.isPending}
              title="End the halt. The engine may open new positions again once it can."
              className="rounded bg-primary px-4 py-2 text-label-md text-on-primary transition-colors duration-base ease-standard hover:bg-primary-container disabled:pointer-events-none disabled:opacity-50"
            >
              {resume.isPending ? 'Resuming…' : 'Resume trading'}
            </button>
          ) : (
            <button
              type="button"
              onClick={() => halt.mutate(HALT_REASON)}
              disabled={halt.isPending}
              title="Stop new entries. Open positions keep their managed exits."
              className="rounded border border-outline px-4 py-2 text-label-md text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-50"
            >
              {halt.isPending ? 'Halting…' : 'Halt'}
            </button>
          )}
          {/* Distinct from Halt in what it does, and inert in this phase for
              a reason stated below rather than on hover alone. */}
          <button
            type="button"
            disabled
            title={FLATTEN_UNAVAILABLE_REASON}
            className="rounded border border-error px-4 py-2 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50"
          >
            Flatten
          </button>
        </div>
      </div>

      <p className="mt-3 max-w-prose text-caption text-on-surface-variant">
        {FLATTEN_UNAVAILABLE_REASON}{' '}
        {positionsQuery.isPending
          ? ''
          : openPositionCount === 1
            ? 'There is 1 open position in this account.'
            : `There are ${openPositionCount} open positions in this account.`}{' '}
        Strategies are still fixtures in this phase — the scanner that would run one arrives with
        the engine, so the selector above records a preference and nothing acts on it yet.
      </p>

      {(halt.isError || resume.isError) && (
        <div className="mt-3">
          <RequestFailed
            error={halt.isError ? halt.error : resume.error}
            what={halt.isError ? 'the halt request' : 'the resume request'}
          />
        </div>
      )}

      {/* One number governs the space between blocks: 32px (DESIGN.md
          `gutter`), down and across alike, so the whitespace framing any card
          measures the same in both directions. */}
      <div className="mt-8 grid grid-cols-1 gap-8 sm:grid-cols-3">
        {accountQuery.isPending || !account ? (
          <>
            <StatCardSkeleton label="Loading total balance" />
            <StatCardSkeleton label="Loading the day's change" />
          </>
        ) : (
          <>
            <StatCard
              label="Total balance"
              // The broker's own equity, and its own day figure below.
              // Deliberately not a sum of the positions on screen: client
              // money arithmetic is display-only, and a total derived from a
              // page of rows is a different number from the one the account
              // holds.
              value={formatUsd(account.equity)}
              icon={<BankIcon />}
              changePct={account.balanceTrend?.changePct}
              comparedTo={account.balanceTrend?.comparedTo}
              note={account.balanceTrend ? undefined : 'No previous close to compare against'}
            />
            <StatCard
              label="Day change"
              // §8.1's "24h performance". It replaces a 24h *volume* card
              // that had no source outside the fixtures — the broker reports
              // no traded-notional figure, and the honest reading of the day
              // is the one it does report.
              //
              // A losing day is `bearish`, never `error`, and carries its
              // sign textually as well as by colour.
              value={formatUsd(account.dayChange, { signed: true })}
              valueClassName={signClass(account.dayChange)}
              icon={account.dayChange < 0 ? <TrendingDownIcon /> : <TrendingUpIcon />}
              note={account.balanceTrend?.comparedTo ?? 'No previous close to compare against'}
            />
          </>
        )}

        {statsQuery.isPending ? (
          <StatCardSkeleton label="Loading the win rate" />
        ) : (
          <WinRateCard stats={statsQuery.data} accountLabel={accountLabel} />
        )}
      </div>

      {accountQuery.isError && (
        <div className="mt-4">
          <RequestFailed error={accountQuery.error} what="the account balance" />
        </div>
      )}

      <section
        aria-labelledby="performance-heading"
        className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest"
      >
        <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
          <h2 id="performance-heading" className="text-title-lg text-on-surface">
            Performance
          </h2>
        </div>
        <div className="p-4">
          <EquityCurve
            isPending={historyQuery.isPending}
            isError={historyQuery.isError}
            error={historyQuery.error}
            points={historyQuery.data?.points}
            t0={historyQuery.data?.t0 ?? null}
            accountLabel={accountLabel}
          />
        </div>
      </section>

      {/* Both panels share the row's height (grid's default stretch) and both
          bodies flex to fill it, so they end level with each other and
          neither leaves a band of empty card below its last row. */}
      <div className="mt-8 grid grid-cols-1 gap-8 lg:grid-cols-2">
        <section
          aria-labelledby="recommended-trades-heading"
          className="flex flex-col rounded-lg border border-outline-warm bg-surface-container-lowest"
        >
          <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
            <h2 id="recommended-trades-heading" className="text-title-lg text-on-surface">
              Recommended Trades
            </h2>
            <div className="flex items-center gap-2">
              <RefreshButton
                label="Refresh the engine state"
                cooldownMs={0}
                onRefresh={() => void engineQuery.refetch()}
              />
              {/* "Open Research", not "View all": with nothing to list, "view
                  all" would invite you to see all of nothing. The link is a
                  destination, not a claim about contents. */}
              <Link
                to="/research"
                className="text-label-md text-primary transition-colors duration-base ease-standard hover:text-on-surface"
              >
                Open Research
              </Link>
            </div>
          </div>
          <NoCandidates
            isPending={engineQuery.isPending}
            isError={engineQuery.isError}
            error={engineQuery.error}
            halted={engineHalted}
            haltedReason={engine?.haltedReason ?? null}
            haltedAt={engine?.haltedAt ?? null}
          />
        </section>

        <section
          aria-labelledby="recent-executions-heading"
          className="flex flex-col rounded-lg border border-outline-warm bg-surface-container-lowest"
        >
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
            <h2 id="recent-executions-heading" className="text-title-lg text-on-surface">
              Recent Executions
            </h2>
            <div className="flex items-center gap-2">
              {/* The status filter goes out on the wire — the server slices
                  by status, this page does not. */}
              <StatusFilterSelect
                value={activityFilter}
                onChange={setActivityFilter}
                label="Filter executions by status"
              />
              <ExportRecentButton items={executionsShown(executionsQuery.data?.items)} />
              <Link
                to="/activity"
                className="text-label-md text-primary transition-colors duration-base ease-standard hover:text-on-surface"
              >
                View all
              </Link>
            </div>
          </div>
          <RecentExecutions
            isPending={executionsQuery.isPending}
            isError={executionsQuery.isError}
            error={executionsQuery.error}
            items={executionsQuery.data?.items}
            filter={activityFilter}
            accountLabel={accountLabel}
          />
        </section>
      </div>
    </div>
  )
}

/** PRD.md §8.1's win-rate stat, over the **realized ledger** rather than a
 * strategy's fixture.
 *
 * §8.1 scopes this figure to the active strategy and compares it to that
 * strategy's backtest. Neither half has a source in this phase: there is no
 * scanner and no strategy runtime, so nothing in the real ledger was opened
 * by a strategy, and the backtest numbers beside it are seeded fixtures. The
 * account-wide rate *is* real, and an honestly narrower true figure beats a
 * precisely-scoped invented one sitting in a row of live money — the same
 * argument decision 16 makes for the panel below.
 *
 * The comparison line is omitted rather than filled in: a trend line is a
 * claim about a baseline, and there is no live baseline to claim. */
function WinRateCard({
  stats,
  accountLabel,
}: {
  stats: ActivityStats | undefined
  accountLabel: string
}) {
  const rate = realizedWinRate(stats)
  const settled = stats ? stats.wins + stats.losses : 0

  return (
    <StatCard
      label="Live win rate"
      value={rate === null ? '—' : `${rate.toFixed(0)}%`}
      valueClassName={rate === null ? 'text-on-surface-variant' : undefined}
      icon={<TargetIcon />}
      note={
        rate === null
          ? `No realized trades in ${accountLabel} yet — a rate over zero trades is unknown, not zero`
          : `${
              settled === 1 ? '1 realized trade' : `${settled} realized trades`
            } in ${accountLabel} · the whole book, not one strategy's`
      }
    />
  )
}

/** The Performance panel's body: skeleton, error, a designed empty state, or
 * the chart.
 *
 * The chart is handed a series, not a response — it is the same component
 * that drew the fixture curve, and the shaping happens here so the chart
 * stays a chart. */
function EquityCurve({
  isPending,
  isError,
  error,
  points,
  t0,
  accountLabel,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  points: readonly EquityCurvePoint[] | undefined
  t0: string | null
  accountLabel: string
}) {
  if (isPending) {
    return <TableSkeleton rows={4} columns={1} label="Loading the equity curve" />
  }
  if (isError || !points) {
    return <RequestFailed error={error} what="the equity curve" />
  }

  const series = equityCurve(points)
  if (series.length < 2) {
    return (
      <p className="max-w-prose text-body-md text-on-surface-variant">
        {series.length === 0
          ? `No equity history for ${accountLabel} yet. The broker reports the curve once the account has been funded and a session has closed on it.`
          : `Only one day of equity history for ${accountLabel} so far — a curve needs two closes to have a shape. It fills in as sessions close.`}
      </p>
    )
  }

  return <PerformanceChart history={series} t0={t0} />
}

/** Spec decision 16 — a Phase 2 surface with no source states why, from real
 * state.
 *
 * There is no scanner and no LLM layer in this phase, so nothing can generate
 * a candidate. The panel is neither hidden (which changes the layout now and
 * changes it back at Phase 4) nor filled with the Phase 1 fixtures (PRD §8.5
 * — a table of invented numbers reads as invented, and those carried strike,
 * expiry and a confidence score). It reads `/api/engine/state` and says what
 * is true: the engine seeds halted on cold start, so "the engine is halted,
 * no candidates are being generated" is assembled from live data.
 *
 * `caution`, never `error` or `bearish`: a halted engine is a correct state,
 * not a fault and not a loss. */
function NoCandidates({
  isPending,
  isError,
  error,
  halted,
  haltedReason,
  haltedAt,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  halted: boolean
  haltedReason: string | null
  haltedAt: string | null
}) {
  if (isPending) {
    return (
      <div className="min-h-0 flex-1 px-4 py-6">
        <ValueSkeleton label="Reading the engine state" className="w-2/3" />
      </div>
    )
  }
  if (isError) {
    return (
      <div className="min-h-0 flex-1 px-4 py-6">
        <RequestFailed error={error} what="the engine state" />
      </div>
    )
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 py-6">
      <span
        className={`w-fit rounded-full px-3 py-1 text-label-md ${
          halted
            ? 'bg-caution-container text-on-caution-container'
            : 'bg-surface-container text-on-surface-variant'
        }`}
      >
        {halted ? 'Engine halted' : 'Engine running'}
      </span>
      <p className="max-w-prose text-body-md text-on-surface">
        {halted
          ? 'The engine is halted, so no candidates are being generated.'
          : 'The engine is running, and has generated no candidates.'}
      </p>
      {halted && (
        <p className="max-w-prose text-body-md text-on-surface-variant">
          {haltedReason ??
            'No reason is recorded: the engine comes up halted on every cold start and stays halted until a human resumes it.'}
        </p>
      )}
      {halted && haltedAt !== null && (
        <p className="text-caption text-on-surface-variant">
          Halted since <span className="text-data-md">{formatDateTimeET(haltedAt)}</span> ET.
        </p>
      )}
      <p className="max-w-prose text-caption text-on-surface-variant">
        Nothing generates trade ideas in this phase — the scanner and the LLM layer that produce
        them arrive with the engine. This panel is empty by construction, not because today
        produced nothing.
      </p>
    </div>
  )
}

/** The executions panel's body: skeleton, error, a designed empty state per
 * reason it is empty, or the table. */
function RecentExecutions({
  isPending,
  isError,
  error,
  items,
  filter,
  accountLabel,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  items: readonly ActivityItem[] | undefined
  filter: ActivityFilter
  accountLabel: string
}) {
  if (isPending) {
    return <TableSkeleton rows={5} columns={5} label="Loading recent executions" />
  }
  if (isError || !items) {
    return (
      <div className="px-4 py-6">
        <RequestFailed error={error} what="recent executions" />
      </div>
    )
  }

  const shown = executionsShown(items)
  if (shown.length === 0) {
    /* Three different facts, said differently. A filter that matched nothing
       is not an account that has never traded, and a window that happens to
       hold only deposits is neither. */
    return (
      <p className="px-4 py-6 text-body-md text-on-surface-variant">
        {filter !== 'all'
          ? `No ${filter} orders in the ${accountLabel} account. Other statuses may have rows — clear the filter to see them.`
          : items.length > 0
            ? `The most recent rows in the ${accountLabel} ledger are all cash movements, which are not executions. Activity shows the whole ledger, transfers included.`
            : `No orders in the ${accountLabel} account yet. Fills, rejections and cancellations land here as they happen.`}
      </p>
    )
  }

  /* No scroll region: the list is capped and simply ends. A short scrollbar
     inside a panel hides how much it holds, and "View all" is the way to the
     rest. `fill` lets the rows take up any slack so this panel ends level
     with Recommended Trades beside it. */
  return (
    <div className="min-h-0 flex-1">
      <ExecutionsTable items={shown} fill />
    </div>
  )
}

/** Exports exactly the rows on screen, and is named for it.
 *
 * Activity's export refetches the whole filtered ledger, because a file
 * called `activity-paper.csv` holding one page would be quietly wrong about
 * how much you traded. This panel is a stated window of ten, the file says
 * `recent-executions`, and the full export lives one link away. */
function ExportRecentButton({ items }: { items: readonly ActivityItem[] }) {
  return (
    <button
      type="button"
      disabled={items.length === 0}
      onClick={() => downloadCsv('recent-executions.csv', activityCsvRows([...items]))}
      className="rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-40"
    >
      Export CSV
    </button>
  )
}
