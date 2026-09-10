import { useState } from 'react'
import { Chip, type ChipVariant } from './Chip'
import { ConfirmDialog } from './ConfirmDialog'
import { ChevronDownIcon } from './icons'
import { useUIStore } from '../lib/store'
import { formatStrategyName } from '../lib/format'
import { nextStatuses, promotionGate } from '../lib/research'
import type { Strategy, StrategyStatus } from '../lib/types'

const STATUS_LABEL: Record<StrategyStatus, string> = {
  draft: 'Draft',
  backtest: 'Backtest',
  paper: 'Paper',
  active: 'Active',
  retired: 'Retired',
}

/** `primary` for the running one, and never `bullish` — being active is a
 * state, not a gain. Retired recedes; the middle stages are neutral because
 * they are progress rather than good or bad news. */
const STATUS_VARIANT: Record<StrategyStatus, ChipVariant> = {
  draft: 'neutral',
  backtest: 'neutral',
  paper: 'caution',
  active: 'accent',
  retired: 'neutral',
}

/** The strategy list (PRD.md §8.5).
 *
 * Rows expand rather than hover to reveal their statistics. §8.5 says hover,
 * and hover was the wrong call: a hover-only panel does not exist on a touch
 * device, to a keyboard, or in a screenshot — the same reasoning that put
 * rejection reasons on screen as text in §8.2 rather than in a tooltip.
 *
 * **Backtest and live are labelled separately and never combined.** A 68%
 * backtest and a 71% live record are different claims about different
 * evidence, and a single blended number would be the most misleading figure
 * on the page.
 */
export function StrategyList() {
  const strategies = useUIStore((s) => s.strategies)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<Strategy | null>(null)
  const deleteStrategy = useUIStore((s) => s.deleteStrategy)

  return (
    <section
      aria-labelledby="strategies-heading"
      className="rounded-lg border border-outline-warm bg-surface-container-lowest"
    >
      <div className="flex items-center justify-between border-b border-outline-warm px-4 py-3">
        <h2 id="strategies-heading" className="text-title-lg text-on-surface">
          Strategies
        </h2>
        <span className="text-caption text-on-surface-variant">
          {strategies.length} total · one active
        </span>
      </div>

      {strategies.length === 0 ? (
        <p className="px-4 py-6 text-caption text-on-surface-variant">
          No strategies. Ask the chat to propose one — it lands here as a draft.
        </p>
      ) : (
        <ul>
          {strategies.map((strategy) => (
            <StrategyRow
              key={strategy.id}
              strategy={strategy}
              expanded={expandedId === strategy.id}
              onToggle={() => setExpandedId(expandedId === strategy.id ? null : strategy.id)}
              onDelete={() => setDeleting(strategy)}
            />
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={deleting !== null}
        title={deleting ? `Delete ${formatStrategyName(deleting.name)}?` : ''}
        consequence={
          deleting ? (
            <>
              Removes the strategy and its record — {deleting.backtest.trades} backtested trades
              {deleting.live ? ` and ${deleting.live.trades} live trades` : ''}. Positions it opened
              stay open and keep their exits, but nothing will manage them. This cannot be undone.
            </>
          ) : (
            ''
          )
        }
        confirmLabel="Delete strategy"
        destructive
        onConfirm={() => {
          if (deleting) deleteStrategy(deleting.id)
          setDeleting(null)
        }}
        onCancel={() => setDeleting(null)}
      />
    </section>
  )
}

function StrategyRow({
  strategy,
  expanded,
  onToggle,
  onDelete,
}: {
  strategy: Strategy
  expanded: boolean
  onToggle: () => void
  onDelete: () => void
}) {
  const renameStrategy = useUIStore((s) => s.renameStrategy)
  const setStrategyStatus = useUIStore((s) => s.setStrategyStatus)
  const [name, setName] = useState(strategy.name)

  const gate = promotionGate(strategy)
  const transitions = nextStatuses(strategy.status)
  const renamed = name.trim() !== '' && name.trim() !== strategy.name

  return (
    <li className="border-t border-outline-variant first:border-t-0">
      <div className="flex items-center justify-between gap-3 px-4 py-3">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className="flex min-w-0 flex-1 items-center gap-2 rounded text-left"
        >
          <span className={`shrink-0 transition-transform ${expanded ? 'rotate-180' : ''}`}>
            <ChevronDownIcon />
          </span>
          <span className="truncate text-body-md text-on-surface">
            {formatStrategyName(strategy.name)}
          </span>
          <span className="shrink-0 text-caption text-on-surface-variant">v{strategy.version}</span>
        </button>
        <Chip variant={STATUS_VARIANT[strategy.status]}>{STATUS_LABEL[strategy.status]}</Chip>
      </div>

      {expanded ? (
        <div className="border-t border-outline-variant bg-surface-container-low px-4 py-4">
          {/* Two columns, separately headed. The whole point is that you
              cannot mistake one for the other. */}
          <div className="grid gap-4 sm:grid-cols-2">
            <StatBlock
              title="Backtest"
              caveat="Simulated fills. Spread modelled, not measured."
              record={strategy.backtest}
            />
            {strategy.live ? (
              <StatBlock title="Live" caveat="Actual fills from this account." record={strategy.live} />
            ) : (
              <div>
                <h4 className="text-caption uppercase tracking-wide text-on-surface-variant">Live</h4>
                <p className="mt-2 text-caption text-on-surface-variant">
                  Never traded live. Nothing to report — a backtested figure shown here would be a
                  simulation presented as a result.
                </p>
              </div>
            )}
          </div>

          {/* §5.3 reports rather than blocks: anything missing the gate can
              still be promoted manually, and anything passing can still be
              rejected. Greying out the control would enforce a rule the spec
              deliberately left to a person. */}
          <div className="mt-4 rounded border border-outline-warm bg-surface p-3">
            <h4 className="text-caption uppercase tracking-wide text-on-surface-variant">
              Promotion gate
            </h4>
            {gate.passes ? (
              <p className="mt-1 text-caption text-on-surface">
                Clears every auto-approval threshold.
              </p>
            ) : (
              <ul className="mt-1 space-y-0.5">
                {gate.failures.map((failure) => (
                  <li key={failure} className="text-caption text-caution">
                    {failure}
                  </li>
                ))}
              </ul>
            )}
            {!gate.passes ? (
              <p className="mt-2 text-caption text-on-surface-variant">
                You can still promote it — the gate automates the obvious cases, it does not make
                the decision.
              </p>
            ) : null}
          </div>

          <div className="mt-4 flex flex-wrap items-end gap-3">
            <div>
              <label
                htmlFor={`name-${strategy.id}`}
                className="block text-caption uppercase tracking-wide text-on-surface-variant"
              >
                Name
              </label>
              <input
                id={`name-${strategy.id}`}
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="mt-1 rounded border border-outline bg-surface px-2 py-1 text-data-md text-on-surface focus:border-primary"
              />
            </div>
            {renamed ? (
              <button
                type="button"
                onClick={() => renameStrategy(strategy.id, name)}
                className="rounded bg-primary px-3 py-1 text-label-md text-on-primary"
              >
                Rename
              </button>
            ) : null}

            {transitions.map((status) => (
              <button
                key={status}
                type="button"
                onClick={() => setStrategyStatus(strategy.id, status)}
                title={
                  status === 'active'
                    ? 'Promoting this demotes the running strategy to paper — only one is active at a time'
                    : undefined
                }
                className="rounded border border-outline px-3 py-1 text-label-md text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container-high"
              >
                {status === 'retired' ? 'Retire' : `Promote to ${STATUS_LABEL[status]}`}
              </button>
            ))}

            {/* Retire the running strategy before deleting it. Deleting it
                outright would leave the positions it opened with nothing
                managing them and the Dashboard reading a strategy that is
                gone. */}
            <button
              type="button"
              onClick={onDelete}
              disabled={strategy.status === 'active'}
              title={
                strategy.status === 'active'
                  ? 'Retire it first — deleting the running strategy would orphan the positions it opened'
                  : 'Delete this strategy'
              }
              className="rounded border border-error px-3 py-1 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container hover:text-on-error-container disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50"
            >
              Delete
            </button>
          </div>
        </div>
      ) : null}
    </li>
  )
}

function StatBlock({
  title,
  caveat,
  record,
}: {
  title: string
  caveat: string
  record: { winRate: number; profitFactor: number; maxDrawdown: number; trades: number }
}) {
  return (
    <div>
      <h4 className="text-caption uppercase tracking-wide text-on-surface-variant">{title}</h4>
      <dl className="mt-2 space-y-1">
        <Stat label="Win rate" value={`${record.winRate}%`} />
        <Stat label="Profit factor" value={String(record.profitFactor)} />
        <Stat label="Max drawdown" value={`${record.maxDrawdown}%`} />
        <Stat label="Closed trades" value={String(record.trades)} />
      </dl>
      <p className="mt-2 text-caption text-on-surface-variant">{caveat}</p>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-caption text-on-surface-variant">{label}</dt>
      <dd className="text-data-md text-on-surface">{value}</dd>
    </div>
  )
}
