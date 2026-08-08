import type { ReactNode } from 'react'

interface StatTileProps {
  label: string
  value: ReactNode
  valueClassName?: string
  /** What the value is computed over — "24 winning trades". An average
   * with no sample size behind it invites more confidence than it has
   * earned, which matters more here than on a balance. */
  note?: string
}

/** A single header stat — label over a large tabular-numeral value. Used
 * across Dashboard, Activity, Account header rows. */
export function StatTile({ label, value, valueClassName, note }: StatTileProps) {
  return (
    <div>
      <p className="text-label-md uppercase tracking-wide text-on-surface-variant">{label}</p>
      <p className={`mt-1 text-data-lg ${valueClassName ?? 'text-on-surface'}`}>{value}</p>
      {note && <p className="mt-1 text-caption text-on-surface-variant">{note}</p>}
    </div>
  )
}
