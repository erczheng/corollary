import { useState } from 'react'
import { Link } from 'react-router-dom'
import { StatCard } from '../components/StatCard'
import { PerformanceChart } from '../components/PerformanceChart'
import { RefreshButton } from '../components/RefreshButton'
import {
  ConfidenceBadge,
  DispositionBadge,
  RecommendationActions,
} from '../components/RecommendationBits'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { ExecutionModeToggle } from '../components/ExecutionModeToggle'
import {
  ExecutionsTable,
  StatusFilterSelect,
  activityCsvRows,
  type ActivityFilter,
} from '../components/ExecutionsTable'
import { BankIcon, ChevronDownIcon, TargetIcon, TrendingUpIcon } from '../components/icons'
import { useUIStore } from '../lib/store'
import {
  dispositionOf,
  visibleRecommendations as visibleRecommendations_,
} from '../lib/research'
import {
  ACCOUNT_SNAPSHOTS,
  isOrderAction,
  LLM_ORIGINATION,
  RECOMMENDATIONS,
  recommendationTitle,
  type Recommendation,
} from '../lib/mockData'
import { downloadCsv } from '../lib/csv'
import {
  formatExpiry,
  formatStrategyName,
  formatUsd,
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

/** The Dashboard shows a recent window, not the whole feed — it's the
 * morning page, and "Recent Executions" that ran to fifty rows would make
 * the "View all" link decorative. Activity paginates the rest. */
const RECENT_EXECUTIONS_LIMIT = 10

export function Dashboard() {
  const activeStrategyId = useUIStore((s) => s.activeStrategyId)
  const setActiveStrategyId = useUIStore((s) => s.setActiveStrategyId)
  const isHalted = useUIStore((s) => s.isHalted)
  const halt = useUIStore((s) => s.halt)
  const resume = useUIStore((s) => s.resume)
  const flatten = useUIStore((s) => s.flatten)
  const accountMode = useUIStore((s) => s.accountMode)
  const executionMode = useUIStore((s) => s.executionMode)
  // Both books are keyed by account. Only the account whose keys are in use
  // is ever on screen — showing paper's positions while Cash is live would
  // misreport real money the same way showing paper's balance would.
  const openPositions = useUIStore((s) => s.openPositions[s.accountMode])
  const activity = useUIStore((s) => s.activity[s.accountMode])
  const [confirmingFlatten, setConfirmingFlatten] = useState(false)
  const [activityFilter, setActivityFilter] = useState<ActivityFilter>('all')
  const [tradeTarget, setTradeTarget] = useState<Recommendation | null>(null)

  /* Recommendation state lives in the store, not here. Research's full table
     acts on the same candidate set, and two components each holding their own
     idea of what was dismissed would disagree the moment you dismissed on one
     and looked at the other. */
  const dispositions = useUIStore((s) => s.dispositions)
  const executeRecommendation = useUIStore((s) => s.executeRecommendation)
  const dismissRecommendation = useUIStore((s) => s.dismissRecommendation)
  const refreshRecommendations = useUIStore((s) => s.refreshRecommendations)

  const visibleRecommendations = visibleRecommendations_(RECOMMENDATIONS, dispositions)

  /* From the store: Research renames, promotes, retires and deletes these, and
     a dropdown reading the fixture would keep offering a strategy that no
     longer exists under a name that has changed. */
  const strategies = useUIStore((s) => s.strategies)
  const activeStrategy = strategies.find((s) => s.id === activeStrategyId) ?? strategies[0]

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

  // Orders only. A deposit isn't an execution — it has no contract, no
  // quantity and no fill — and mixing cash movements into a trading feed
  // makes "what did the engine do today" harder to read at a glance. The
  // full ledger, deposits included, is on Activity.
  const orders = activity.filter((a) => isOrderAction(a.action))
  const filteredActivity =
    activityFilter === 'all' ? orders : orders.filter((a) => a.status === activityFilter)
  const recentActivity = filteredActivity.slice(0, RECENT_EXECUTIONS_LIMIT)

  // Halt is a statement about the engine: it stops the engine opening new
  // positions. In Manual the engine opens nothing to begin with, so the
  // halted state has nothing to say and isn't shown or offered. It stays in
  // the store either way — flatten still sets it, and switching to Auto
  // surfaces it rather than quietly resuming (CLAUDE.md rule 9).
  const isAuto = executionMode === 'auto'
  const engineHalted = isAuto && isHalted

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-3">
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

      <div className="mt-8 flex flex-wrap items-center gap-3">
        <AccountModeToggle />
        <ExecutionModeToggle />
        <div className="relative shrink-0">
          <select
            value={activeStrategyId}
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

      {/* One number governs the space between blocks: 32px (DESIGN.md
          `gutter`), down and across alike, so the whitespace framing any card
          measures the same in both directions. Four 8px steps — this briefly
          sat at 36px, which is four and a half, and nothing else in the
          system lands between steps.

          The page's side padding is deliberately *not* that number — it is
          48px, so the page reads as inset from the window by more than its
          cards are separated from each other. Equal would flatten the two
          into one undifferentiated field.

          Both values are a departure from DESIGN.md's original "use lg (48px)
          to separate major sections"; the doc's Layout section has been
          rewritten to match rather than left to contradict the app. */}
      <div className="mt-8 grid grid-cols-1 gap-8 sm:grid-cols-3">
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
        {/* PRD.md §8.1 scopes this to *validated* trades, excluding
            LLM-originated `unvalidated` ones. The figure was already validated
            — a strategy trades its own setups, and a setup match is what makes
            a trade validated (§6.2), so an unvalidated origination has no
            strategy to be counted against in the first place. What was missing
            was saying so. The excluded count comes from the account-wide
            origination bucket, which is the only place those trades exist. */}
        <StatCard
          label="Live win rate"
          value={live ? `${live.winRate}%` : '—'}
          icon={<TargetIcon />}
          changePct={winRateDelta}
          changeUnit="pts"
          comparedTo={
            live
              ? `vs backtest · ${live.trades} validated · ${LLM_ORIGINATION.unvalidated} excluded`
              : undefined
          }
          note={`Not traded live yet — backtested ${activeStrategy.backtest.winRate}% over ${activeStrategy.backtest.trades} trades`}
        />
      </div>

      {/* Same shell as Recommended Trades and Recent Executions below, and
          as every section on Activity: a bordered header row, then a body.
          It used to be a plain p-6 box, which put this h2 8px right of every
          other heading on the page — a shelf you could see. */}
      <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
        <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
          <h2 className="text-title-lg text-on-surface">Performance</h2>
        </div>
        <div className="p-4">
          <PerformanceChart history={account.portfolioHistory} />
        </div>
      </section>

      {/* Both panels share the row's height (grid's default stretch) and
          both bodies flex to fill it, so they end level with each other and
          neither leaves a band of empty card below its last row. Stretch
          alone caused that gap; stretch plus a filling body is what removes
          it. */}
      <div className="mt-8 grid grid-cols-1 gap-8 lg:grid-cols-2">
        <section className="flex flex-col rounded-lg border border-outline-warm bg-surface-container-lowest">
          <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
            <h2 className="text-title-lg text-on-surface">Recommended Trades</h2>
            <div className="flex items-center gap-2">
              {/* A refresh restores anything dismissed — the scanner
                  rebuilds its candidate set, it doesn't remember what you
                  waved off. */}
              <RefreshButton onRefresh={refreshRecommendations} />
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
            /* flex-1 to fill the shared row height, min-h-0 so the scroll
               actually engages inside a flex column, and a ceiling so a long
               candidate list can't drive the whole row taller than the
               screen. */
            <div className="min-h-0 max-h-[32rem] flex-1 overflow-y-auto no-scrollbar">
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
                    <div className="mt-1 flex min-w-0 items-center gap-2">
                      <span className="truncate text-caption text-on-surface-variant">
                        {r.reason}
                      </span>
                      <span className="shrink-0 text-caption text-on-surface-variant">
                        · Exp {formatExpiry(r.expiry)}
                      </span>
                    </div>
                  </div>

                  {/* Confidence and the actions are shared with Research's
                      full table — see RecommendationBits. The confidence slot
                      in particular has three states that are easy to get
                      subtly wrong, and it had no business existing twice.
                      `compact` shortens Execute to Trade, which is the
                      wording §8.1 uses for this panel. */}
                  <div className="flex shrink-0 items-center gap-2">
                    <ConfidenceBadge recommendation={r} />
                    <DispositionBadge disposition={dispositionOf(r.id, dispositions)} />
                    <RecommendationActions
                      recommendation={r}
                      disposition={dispositionOf(r.id, dispositions)}
                      halted={engineHalted}
                      compact
                      onExecute={() => setTradeTarget(r)}
                      onDismiss={() => dismissRecommendation(r.id)}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="flex flex-col rounded-lg border border-outline-warm bg-surface-container-lowest">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
            <h2 className="text-title-lg text-on-surface">Recent Executions</h2>
            <div className="flex items-center gap-2">
              <StatusFilterSelect
                value={activityFilter}
                onChange={setActivityFilter}
                label="Filter executions by status"
              />
              {/* Exports everything the filter matched, not just the ten
                  rows on screen — the visible window is a reading
                  convenience, and silently truncating an export is how you
                  reconcile against the broker and come up short. */}
              <button
                type="button"
                onClick={() => downloadCsv('recent-executions.csv', activityCsvRows(filteredActivity))}
                className="rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
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
          {recentActivity.length === 0 ? (
            <p className="px-4 py-6 text-body-md text-on-surface-variant">
              No {activityFilter === 'all' ? '' : `${activityFilter} `}orders in this account yet.
            </p>
          ) : (
            /* No scroll region: the list is capped at RECENT_EXECUTIONS_LIMIT
               rows and simply ends. A short scrollbar inside a panel hides
               how much it holds, and "View all" is the way to the rest.
               `fill` lets the rows take up any slack so this panel ends level
               with Recommended Trades beside it. */
            <div className="min-h-0 flex-1">
              <ExecutionsTable items={recentActivity} fill />
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
          if (tradeTarget) executeRecommendation(tradeTarget.id)
          setTradeTarget(null)
        }}
        onCancel={() => setTradeTarget(null)}
      />
    </div>
  )
}
