import type { ReactNode } from 'react'
import { ArrowDownIcon, ArrowUpIcon } from './icons'

interface StatCardProps {
  label: string
  value: string
  icon: ReactNode
  /** Omit when the stat has nothing to compare against — pass `note`
   * instead. A trend line is a claim about a baseline, so inventing one
   * where no baseline exists is worse than showing none. */
  changePct?: number
  comparedTo?: string
  /** Percentage points, not percent, when the value is itself a percentage:
   * 71% against a 68% baseline is +3 pts, not +3%. */
  changeUnit?: '%' | 'pts'
  /** Shown in place of the trend line when `changePct` is omitted. */
  note?: string
}

/** A header stat as a card: label + icon, a large tabular-numeral value,
 * and a signed trend line. Money/percent values stay in `data-*`
 * (JetBrains Mono, tabular-nums) even at this larger size — CLAUDE.md's
 * "every price, quantity, and percentage" rule doesn't stop applying just
 * because the number got bigger. */
export function StatCard({
  label,
  value,
  icon,
  changePct,
  comparedTo,
  changeUnit = '%',
  note,
}: StatCardProps) {
  const positive = changePct !== undefined && changePct >= 0

  return (
    <div
      role="group"
      aria-label={label}
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <div className="flex items-center justify-between">
        <p className="text-label-md uppercase tracking-wide text-on-surface-variant">{label}</p>
        <span className="text-on-surface-variant">{icon}</span>
      </div>
      <p className="mt-1 text-data-xl text-on-surface">{value}</p>
      {changePct !== undefined ? (
        <div className="mt-1 flex items-center gap-2 text-label-md">
          <span className={positive ? 'text-bullish' : 'text-bearish'}>
            {positive ? <ArrowUpIcon /> : <ArrowDownIcon />}
          </span>
          <span className={positive ? 'text-bullish' : 'text-bearish'}>
            {positive ? '+' : '−'}
            {Math.abs(changePct).toFixed(1)}
            {changeUnit === '%' ? '%' : ' pts'}
          </span>
          <span className="text-on-surface-variant">{comparedTo}</span>
        </div>
      ) : (
        // Same height as the trend line so a card without one doesn't make
        // the row of cards ragged.
        <p className="mt-1 text-label-md text-on-surface-variant">{note ?? ' '}</p>
      )}
    </div>
  )
}
