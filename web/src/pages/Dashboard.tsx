import { useState } from 'react'
import { Link } from 'react-router-dom'
import { StatTile } from '../components/StatTile'
import { PerformanceChart } from '../components/PerformanceChart'
import { RefreshButton } from '../components/RefreshButton'
import { Chip } from '../components/Chip'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { useUIStore } from '../lib/store'
import {
  ACTIVITY_STATUS_LABEL,
  OPEN_POSITIONS,
  PORTFOLIO_HISTORY,
  RECENT_ACTIVITY,
  RECOMMENDATIONS,
  STRATEGIES,
  VOLUME_24H,
  type ActivityStatus,
} from '../lib/mockData'
import { downloadCsv } from '../lib/csv'
import { formatDateTimeET, formatUsd, signClass } from '../lib/format'

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
  const [confirmingFlatten, setConfirmingFlatten] = useState(false)
  const [activityFilter, setActivityFilter] = useState<(typeof ACTIVITY_FILTERS)[number]>('all')

  const activeStrategy = STRATEGIES.find((s) => s.id === activeStrategyId) ?? STRATEGIES[0]
  const balance = PORTFOLIO_HISTORY[PORTFOLIO_HISTORY.length - 1].value
  const winRate = activeStrategy.live?.winRate ?? activeStrategy.backtest.winRate

  const filteredActivity =
    activityFilter === 'all' ? RECENT_ACTIVITY : RECENT_ACTIVITY.filter((a) => a.status === activityFilter)

  return (
    <div className="mx-auto max-w-[1140px] px-4 py-12 lg:px-16">
      <div className="flex flex-wrap items-start justify-between gap-6">
        <div className="flex gap-8">
          <StatTile label="Total balance" value={formatUsd(balance)} />
          <StatTile label="24h volume" value={formatUsd(VOLUME_24H)} />
          <StatTile
            label="Strategy win rate"
            value={`${winRate}%`}
            valueClassName={winRate >= 50 ? 'text-bullish' : 'text-bearish'}
          />
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {isHalted && <Chip variant="caution">Halted</Chip>}
          <label className="flex items-center gap-2 text-label-md text-on-surface-variant">
            Strategy
            <select
              value={activeStrategyId}
              onChange={(e) => setActiveStrategyId(e.target.value)}
              className="rounded border border-outline bg-surface px-2 py-1.5 text-body-md text-on-surface focus:border-primary"
            >
              {STRATEGIES.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} (v{s.version})
                </option>
              ))}
            </select>
          </label>
          {isHalted ? (
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
          )}
          <button
            type="button"
            onClick={() => setConfirmingFlatten(true)}
            className="rounded border border-error px-4 py-2 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container"
          >
            Flatten
          </button>
        </div>
      </div>

      <section className="mt-12 rounded-lg border border-outline-warm bg-surface-container-lowest p-6">
        <h2 className="text-title-lg text-on-surface">Performance</h2>
        <div className="mt-3">
          <PerformanceChart />
        </div>
      </section>

      <div className="mt-12 grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section className="rounded-lg border border-outline-warm bg-surface-container-lowest">
          <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
            <h2 className="text-title-lg text-on-surface">Recommended Trades</h2>
            <div className="flex items-center gap-2">
              <RefreshButton onRefresh={() => {}} />
              <Link
                to="/research"
                className="text-label-md text-primary transition-colors duration-base ease-standard hover:text-on-surface"
              >
                View all
              </Link>
            </div>
          </div>
          {RECOMMENDATIONS.length === 0 ? (
            <p className="px-4 py-6 text-body-md text-on-surface-variant">{recommendationsEmptyMessage()}</p>
          ) : (
            <div className="max-h-80 overflow-y-auto no-scrollbar">
              {RECOMMENDATIONS.map((r) => (
                <div
                  key={r.id}
                  className="flex items-center justify-between gap-3 border-t border-outline/10 px-4 py-3 first:border-t-0 hover:bg-surface-container-low"
                >
                  <div className="min-w-0">
                    <p className="truncate text-body-md text-on-surface">
                      {r.symbol} — {r.contract}
                    </p>
                    <p className="text-caption text-on-surface-variant">{r.setup.replace(/_/g, ' ')}</p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {r.unvalidated && <Chip variant="accent">Unvalidated</Chip>}
                    <span className="text-data-md text-on-surface">
                      {r.confidence !== null ? `${r.confidence}%` : '—'}
                    </span>
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
                <tbody>
                  {filteredActivity.map((a) => (
                    <tr key={a.id} className="border-t border-outline/10 first:border-t-0 hover:bg-surface-container-low">
                      <td className="px-4 py-3 text-caption text-on-surface-variant">{formatDateTimeET(a.time)}</td>
                      <td className="px-4 py-3 text-body-md text-on-surface">
                        {a.action} {a.contract !== '—' ? a.contract : ''}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right text-data-md text-on-surface">
                        {a.pnl !== null ? (
                          <span className={signClass(a.pnl)}>{formatUsd(a.pnl, { signed: true })}</span>
                        ) : (
                          '—'
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
            Closes all {OPEN_POSITIONS.length} open positions at market, then halts new entries. Existing managed
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
    </div>
  )
}
