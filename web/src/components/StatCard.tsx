import type { ReactNode } from 'react'
import { ArrowDownIcon, ArrowUpIcon } from './icons'

interface StatCardProps {
  label: string
  /** A node, not a string, so a card can pair a headline figure with a
   * secondary one — Activity's "+$141.00 +31.90%". Keep the tabular
   * numerals on both halves. */
  value: ReactNode
  /** Overrides the value colour. Activity's average win is `bullish` and
   * its average loss `bearish`; the Dashboard's balances stay neutral
   * because a balance has no sign to report. Never `error` — a losing
   * trade is not a system failure. */
  valueClassName?: string
  icon: ReactNode
  /** Omit when the stat has nothing to compare against — pass `note`
   * instead. A trend line is a claim about a baseline, so inventing one
   * where no baseline exists is worse than showing none. */
  changePct?: number
  comparedTo?: string
  /** Percentage points, not percent, when the value is itself a percentage:
   * 71% against a 68% baseline is +3 pts, not +3%. */
  changeUnit?: '%' | 'pts'
  /** Whether up is good.
   *
   * `directional` (the default) colours a rise `bullish` and a fall
   * `bearish`, which is right for a balance or a win rate. `neutral` keeps
   * the arrow — direction is still a fact — but drops the colour, for a
   * figure where up is not better: **the VIX is the case this exists for.**
   * A spiking VIX painted green would report risk-off as a gain, the same
   * class of error as colouring a losing position `error`. */
  trendTone?: 'directional' | 'neutral'
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
  valueClassName,
  icon,
  changePct,
  comparedTo,
  changeUnit = '%',
  trendTone = 'directional',
  note,
}: StatCardProps) {
  const positive = changePct !== undefined && changePct >= 0
  const toneClass =
    trendTone === 'neutral' ? 'text-on-surface-variant' : positive ? 'text-bullish' : 'text-bearish'

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
      <p className={`mt-1 text-data-xl ${valueClassName ?? 'text-on-surface'}`}>{value}</p>
      {changePct !== undefined ? (
        <div className="mt-1 flex items-center gap-2 text-label-md">
          <span className={toneClass}>
            {positive ? <ArrowUpIcon /> : <ArrowDownIcon />}
          </span>
          <span className={`whitespace-nowrap ${toneClass}`}>
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
