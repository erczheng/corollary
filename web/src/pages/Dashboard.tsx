import { useState } from 'react'
import { Link } from 'react-router-dom'
import { StatCard } from '../components/StatCard'
import { PerformanceChart } from '../components/PerformanceChart'
import { RefreshButton } from '../components/RefreshButton'
import { Chip } from '../components/Chip'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { ExecutionModeToggle } from '../components/ExecutionModeToggle'
import { BankIcon, ChevronDownIcon, TargetIcon, TrendingUpIcon, XIcon } from '../components/icons'
import { useUIStore } from '../lib/store'
import {
  ACCOUNT_SNAPSHOTS,
  ACTIVITY_ACTION_LABEL,
  ACTIVITY_STATUS_CLASS,
  ACTIVITY_STATUS_LABEL,
  RECOMMENDATIONS,
  recommendationTitle,
  STRATEGIES,
  type ActivityStatus,
  type Recommendation,
} from '../lib/mockData'
import { downloadCsv } from '../lib/csv'
import {
  CONFIDENCE_TIER_CLASS,
  confidenceTier,
  formatDateTimeET,
  formatExpiry,
  formatStrategyName,
  formatUsd,
  signClass,
} from '../lib/format'

function recommendationsEmptyMessage(): string {
  const hourET = Number(
    new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour: 'numeric', hour12: false }).format(
      new Date(),
    ),
  )
  return hourET < 9
    ? 'Building today’s candidates.'
    : 'No candidates meet the active strategy’s criteria.'
}

const ACTIVITY_FILTERS: (ActivityStatus | 'all')[] = ['all', 'filled', 'rejected', 'pending', 'canceled']

export function Dashboard() {
  const activeStrategyId = useUIStore((s) => s.activeStrategyId)
  const setActiveStrategyId = useUIStore((s) => s.setActiveStrategyId)
  const isHalted = useUIStore((s) => s.isHalted)
  const halt = useUIStore((s) => s.halt)
  const resume = useUIStore((s) => s.resume)
  const flatten = useUIStore((s) => s.flatten)
  const accountMode = useUIStore((s) => s.accountMode)
  const executionMode = useUIStore((s) => s.executionMode)
  const openPositions = useUIStore((s) => s.openPositions)
  const activity = useUIStore((s) => s.activity)
  const [confirmingFlatten, setConfirmingFlatten] = useState(false)
  const [activityFilter, setActivityFilter] = useState<(typeof ACTIVITY_FILTERS)[number]>('all')
  const [dismissedIds, setDismissedIds] = useState<string[]>([])
  const [tradeTarget, setTradeTarget] = useState<Recommendation | null>(null)

  const visibleRecommendations = RECOMMENDATIONS.filter((r) => !dismissedIds.includes(r.id))

  const activeStrategy = STRATEGIES.find((s) => s.id === activeStrategyId) ?? STRATEGIES[0]

  // Paper and Cash are different accounts holding different money, so the
  // balance, the volume, and the chart all follow the toggle.
  const account = ACCOUNT_SNAPSHOTS[accountMode]
  const balance = account.portfolioHistory[account.portfolioHistory.length - 1].value

  // PRD.md §8.1 specifies this stat as the *live* win rate. A strategy that
  // has never traded live has no live win rate, and showing its backtested
  // number in the same slot would present a simulation as a result — the
  // backtest goes in the note underneath instead, where it's labeled.
  const live = activeStrategy.live
  const winRateDelta = live ? live.winRate - activeStrategy.backtest.winRate : undefined

  const filteredActivity =
    activityFilter === 'all' ? activity : activity.filter((a) => a.status === activityFilter)

  // Halt is a statement about the engine: it stops the engine opening new
  // positions. In Manual the engine opens nothing to begin with, so the
  // halted state has nothing to say and isn't shown or offered. It stays in
  // the store either way — flatten still sets it, and switching to Auto
  // surfaces it rather than quietly resuming (CLAUDE.md rule 9).
  const isAuto = executionMode === 'auto'
  const engineHalted = isAuto && isHalted

  return (
    <div className="mx-auto max-w-[1140px] px-4 py-12 lg:px-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-display-lg text-on-surface">Portfolio Overview</h1>
        {/* The trading-state pill — primary, per DESIGN.md's Colors section
            ("the trading-state pill"). Read-only: it reports whether the
            engine is placing orders on its own and which account is live.
            The controls that change either of those are in the row below.

            "Trading On" is reserved for Auto, because that is the only
            state in which the engine acts unattended. In Manual it says
            Manual — claiming the terminal is "trading" when nothing moves
            without a click is the sort of overstatement you'd only notice
            the day it mattered.

            The status dot uses the *-container semantics rather than bare
            `bullish`/`caution`/`neutral` because those sit at nearly the
            same lightness as `primary` in both themes, which made the dot
            almost invisible on the pill's fill. */}
        <span className="flex items-center gap-2 whitespace-nowrap rounded-full bg-primary px-4 py-2 text-label-md text-on-primary">
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${
              !isAuto
                ? 'bg-neutral-container'
                : isHalted
                  ? 'bg-caution-container'
                  : 'bg-bullish-container'
            }`}
            aria-hidden="true"
          />
          {!isAuto ? 'Manual' : isHalted ? 'Halted' : 'Trading On'} (
          {accountMode === 'cash' ? 'Cash' : 'Paper'})
        </span>
      </div>

      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Monitor your total balance, 24h performance, and today's recommended trades.
      </p>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <AccountModeToggle />
        <ExecutionModeToggle />
        <div className="relative shrink-0">
          <select
            value={activeStrategyId}
            onChange={(e) => setActiveStrategyId(e.target.value)}
            aria-label="Active strategy"
            className="appearance-none whitespace-nowrap rounded-full border border-outline bg-surface-container-low py-2 pl-4 pr-9 text-label-md text-on-surface focus:border-primary"
          >
            {STRATEGIES.map((s) => (
              <option key={s.id} value={s.id}>
                Strategy: {formatStrategyName(s.name)}
              </option>
            ))}
          </select>
          <ChevronDownIcon className="pointer-events-none absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-on-surface-variant" />
        </div>

        {/* Actions sit apart from the settings above so Flatten — the one
            destructive control on this page — isn't adjacent to the
            toggles you'd click casually. */}
        <div className="flex items-center gap-3 sm:ml-auto">
          {/* Halt only appears in Auto. There is nothing for it to stop in
              Manual, and a control that does nothing visible is worse than
              an absent one. Flatten stays in both modes — open positions
              are open regardless of how they were opened. */}
          {isAuto &&
            (isHalted ? (
              <button
                type="button"
                onClick={resume}
                className="rounded bg-primary px-4 py-2 text-label-md text-on-primary transition-colors duration-base ease-standard hover:bg-primary-container"
              >
                Resume trading
              </button>
            ) : (
              <button
                type="button"
                onClick={halt}
                className="rounded border border-outline px-4 py-2 text-label-md text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-low"
              >
                Halt
              </button>
            ))}
          {/* Distinct from Halt in what it does, so also distinct in when
              it's available: with nothing open there is nothing to close,
              and Halt remains the control that stops new entries. */}
          <button
            type="button"
            onClick={() => setConfirmingFlatten(true)}
            disabled={openPositions.length === 0}
            title={
              openPositions.length === 0
                ? 'No open positions to close'
                : `Close all ${openPositions.length} open positions, then halt`
            }
            className="rounded border border-error px-4 py-2 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50"
          >
            Flatten
          </button>
        </div>
      </div>

      <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-3">
        <StatCard
          label="Total balance"
          value={formatUsd(balance)}
          icon={<BankIcon />}
          changePct={account.balanceTrend.changePct}
          comparedTo={account.balanceTrend.comparedTo}
        />
        <StatCard
          label="24h volume"
          value={formatUsd(account.volume24h)}
          icon={<TrendingUpIcon />}
          changePct={account.volumeTrend.changePct}
          comparedTo={account.volumeTrend.comparedTo}
        />
        <StatCard
          label="Live win rate"
          value={live ? `${live.winRate}%` : '—'}
          icon={<TargetIcon />}
          changePct={winRateDelta}
          changeUnit="pts"
          comparedTo={live ? `vs backtest, ${live.trades} live trades` : undefined}
          note={`Not traded live yet — backtested ${activeStrategy.backtest.winRate}% over ${activeStrategy.backtest.trades} trades`}
        />
      </div>

      <section className="mt-12 rounded-lg border border-outline-warm bg-surface-container-lowest p-6">
        <h2 className="text-title-lg text-on-surface">Performance</h2>
        <div className="mt-3">
          <PerformanceChart history={account.portfolioHistory} />
        </div>
      </section>

      <div className="mt-12 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="rounded-lg border border-outline-warm bg-surface-container-lowest">
          <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
            <h2 className="text-title-lg text-on-surface">Recommended Trades</h2>
            <div className="flex items-center gap-2">
              {/* A refresh restores anything dismissed — the scanner
                  rebuilds its candidate set, it doesn't remember what you
                  waved off. */}
              <RefreshButton onRefresh={() => setDismissedIds([])} />
              <Link
                to="/research"
                className="text-label-md text-primary transition-colors duration-base ease-standard hover:text-on-surface"
              >
                View all
              </Link>
            </div>
          </div>
          {visibleRecommendations.length === 0 ? (
            <p className="px-4 py-6 text-body-md text-on-surface-variant">{recommendationsEmptyMessage()}</p>
          ) : (
            <div className="max-h-80 overflow-y-auto no-scrollbar">
              {visibleRecommendations.map((r) => (
                <div
                  key={r.id}
                  className="flex items-center justify-between gap-3 border-t border-outline/10 px-4 py-3 first:border-t-0 hover:bg-surface-container-low"
                >
                  <div className="min-w-0">
                    <p className="truncate text-body-md text-on-surface">
                      {recommendationTitle(r)}
                    </p>
                    {/* Reason truncates before the expiry does — a clipped
                        rationale is a nuisance, a clipped expiry is
                        misleading. */}
                    <div className="mt-0.5 flex min-w-0 items-center gap-2">
                      <span className="truncate text-caption text-on-surface-variant">
                        {r.reason}
                      </span>
                      <span className="shrink-0 text-caption text-on-surface-variant">
                        · Exp {formatExpiry(r.expiry)}
                      </span>
                    </div>
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    {r.confidence !== null ? (
                      <span
                        className={`rounded-full px-2.5 py-1 text-data-md ${
                          CONFIDENCE_TIER_CLASS[confidenceTier(r.confidence)]
                        }`}
                        title={`${confidenceTier(r.confidence)} confidence — backtested hit rate for this setup class`}
                      >
                        {r.confidence}%
                      </span>
                    ) : r.unvalidated ? (
                      /* An unvalidated origination has no base rate by
                         construction (PRD.md §6.2 — no setup match), so the
                         tag goes in the slot the number would have taken
                         rather than competing with the reason text. It says
                         the same thing the em dash would, with the reason
                         why. */
                      <Chip
                        variant="accent"
                        title="Unvalidated — no setup match, so no backtested base rate. Capped at ⅓ normal size."
                      >
                        Untested
                      </Chip>
                    ) : (
                      /* PRD.md §6.3: where no base rate exists, confidence
                         shows an em dash rather than a number. */
                      <span
                        className="px-2.5 py-1 text-data-md text-on-surface-variant"
                        title="No backtested base rate for this setup yet"
                      >
                        —
                      </span>
                    )}

                    <button
                      type="button"
                      onClick={() => setTradeTarget(r)}
                      disabled={engineHalted}
                      title={
                        engineHalted
                          ? 'Trading is halted — resume to open new positions'
                          : `Trade ${recommendationTitle(r)}`
                      }
                      className="rounded border border-primary px-3 py-1 text-label-md text-primary transition-colors duration-base ease-standard hover:bg-primary-container hover:text-on-primary-container disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50"
                    >
                      Trade
                    </button>
                    <button
                      type="button"
                      onClick={() => setDismissedIds((ids) => [...ids, r.id])}
                      aria-label={`Dismiss ${recommendationTitle(r)}`}
                      title="Dismiss"
                      className="flex h-7 w-7 items-center justify-center rounded-full text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-high hover:text-on-surface"
                    >
                      <XIcon className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-lg border border-outline-warm bg-surface-container-lowest">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
            <h2 className="text-title-lg text-on-surface">Recent Executions</h2>
            <div className="flex items-center gap-2">
              <select
                value={activityFilter}
                onChange={(e) => setActivityFilter(e.target.value as (typeof ACTIVITY_FILTERS)[number])}
                aria-label="Filter executions by status"
                className="rounded border border-outline bg-surface px-2 py-1.5 text-label-md text-on-surface focus:border-primary"
              >
                {ACTIVITY_FILTERS.map((f) => (
                  <option key={f} value={f}>
                    {f === 'all' ? 'All statuses' : ACTIVITY_STATUS_LABEL[f]}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() =>
                  downloadCsv(
                    'recent-executions.csv',
                    filteredActivity.map((a) => ({
                      time: a.time,
                      contract: a.contract,
                      action: a.action,
                      price: a.price ?? '',
                      quantity: a.quantity ?? '',
                      pnl: a.pnl ?? '',
                      amount: a.amount ?? '',
                      status: a.status,
                    })),
                  )
                }
                className="rounded border border-outline px-3 py-1.5 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
              >
                Export CSV
              </button>
              <Link
                to="/activity"
                className="text-label-md text-primary transition-colors duration-base ease-standard hover:text-on-surface"
              >
                View all
              </Link>
            </div>
          </div>
          {filteredActivity.length === 0 ? (
            <p className="px-4 py-6 text-body-md text-on-surface-variant">No activity matches this filter.</p>
          ) : (
            <div className="max-h-80 overflow-y-auto no-scrollbar">
              <table className="w-full border-collapse">
                <thead>
                  {/* Sticky so the columns stay identifiable while the
                      body scrolls inside the panel. */}
                  <tr className="sticky top-0 bg-surface-container">
                    <th className="whitespace-nowrap px-3 py-2 text-left text-label-md uppercase text-on-surface-variant">
                      Time
                    </th>
                    {/* w-full lets this column absorb the table's slack so
                        the contract truncates as late as possible; it's
                        the column carrying the most information. */}
                    <th className="w-full px-3 py-2 text-left text-label-md uppercase text-on-surface-variant">
                      Asset / Action
                    </th>
                    <th className="px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">Qty</th>
                    <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filteredActivity.map((a) => (
                    <tr key={a.id} className="border-t border-outline/10 hover:bg-surface-container-low">
                      <td className="whitespace-nowrap px-3 py-2 text-caption text-on-surface-variant">
                        {formatDateTimeET(a.time)}
                      </td>
                      <td
                        className="max-w-0 truncate px-3 py-2 text-body-md text-on-surface"
                        title={a.contract === '—' ? undefined : a.contract}
                      >
                        {a.contract === '—'
                          ? ACTIVITY_ACTION_LABEL[a.action]
                          : `${ACTIVITY_ACTION_LABEL[a.action]} ${a.contract}`}
                      </td>
                      <td className="px-3 py-2 text-right text-data-md text-on-surface">
                        {a.quantity ?? '—'}
                      </td>
                      {/* The most specific thing known about the row: its
                          P&L if the trade produced one, the cash moved if
                          it was a deposit or withdrawal, otherwise the
                          status word. An opening fill has no realized P&L
                          yet, and a rejection never will. */}
                      <td className="whitespace-nowrap px-3 py-2 text-right">
                        {a.pnl !== null ? (
                          <span className={`text-data-md ${signClass(a.pnl)}`}>
                            {formatUsd(a.pnl, { signed: true })}
                          </span>
                        ) : a.amount !== null ? (
                          /* Deliberately not signClass: a deposit is money
                             you moved, not money the account made, and
                             rendering it bullish green would read as a
                             gain. The sign still carries the direction. */
                          <span
                            className="text-data-md text-on-surface"
                            title={`${ACTIVITY_ACTION_LABEL[a.action]} — ${ACTIVITY_STATUS_LABEL[a.status]}`}
                          >
                            {formatUsd(a.amount, { signed: true })}
                          </span>
                        ) : (
                          <span
                            className={`text-caption ${ACTIVITY_STATUS_CLASS[a.status]}`}
                            title={a.rejectionReason}
                          >
                            {ACTIVITY_STATUS_LABEL[a.status]}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={confirmingFlatten}
        title="Flatten all positions?"
        consequence={
          <>
            Closes all {openPositions.length} open positions at market, then halts new entries. Existing managed
            exits on any position that doesn't fill immediately are canceled.
          </>
        }
        confirmLabel="Flatten"
        destructive
        onConfirm={() => {
          flatten()
          setConfirmingFlatten(false)
        }}
        onCancel={() => setConfirmingFlatten(false)}
      />

      <ConfirmDialog
        open={tradeTarget !== null}
        title="Place this trade?"
        consequence={
          tradeTarget && (
            <>
              Submits {recommendationTitle(tradeTarget)}, expiring {formatExpiry(tradeTarget.expiry)}, to the
              risk manager for sizing and
              approval, using your {accountMode === 'cash' ? 'Cash' : 'Paper'} account. It is rejected,
              with the reason logged to Activity, if it breaches a risk limit.
            </>
          )
        }
        confirmLabel="Place trade"
        onConfirm={() => {
          /* Phase 1 is mock data — nothing is submitted. When this is
             wired up in Phase 6, it goes through RiskManager.approve()
             and nowhere else (CLAUDE.md rule 1). Do not call the broker
             from this handler. */
          if (tradeTarget) setDismissedIds((ids) => [...ids, tradeTarget.id])
          setTradeTarget(null)
        }}
        onCancel={() => setTradeTarget(null)}
      />
    </div>
  )
}
