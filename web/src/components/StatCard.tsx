import type { ReactNode } from 'react'
import { ArrowDownIcon, ArrowUpIcon } from './icons'

interface StatCardProps {
  label: string
  value: string
  icon: ReactNode
  changePct: number
  comparedTo: string
}

/** A header stat as a card: label + icon, a large tabular-numeral value,
 * and a signed trend line. Money/percent values stay in `data-*`
 * (JetBrains Mono, tabular-nums) even at this larger size — CLAUDE.md's
 * "every price, quantity, and percentage" rule doesn't stop applying just
 * because the number got bigger. */
export function StatCard({ label, value, icon, changePct, comparedTo }: StatCardProps) {
  const positive = changePct >= 0

  return (
    <div className="rounded-lg border border-outline-warm bg-surface-container-lowest p-6">
      <div className="flex items-center justify-between">
        <p className="text-label-md uppercase tracking-wide text-on-surface-variant">{label}</p>
        <span className="text-on-surface-variant">{icon}</span>
      </div>
      <p className="mt-3 text-data-xl text-on-surface">{value}</p>
      <div className="mt-2 flex items-center gap-1.5 text-label-md">
        <span className={positive ? 'text-bullish' : 'text-bearish'}>
          {positive ? <ArrowUpIcon /> : <ArrowDownIcon />}
        </span>
        <span className={positive ? 'text-bullish' : 'text-bearish'}>
          {positive ? '+' : '−'}
          {Math.abs(changePct).toFixed(1)}%
        </span>
        <span className="text-on-surface-variant">{comparedTo}</span>
      </div>
    </div>
  )
}
