import type { ReactNode } from 'react'

interface StatTileProps {
  label: string
  value: ReactNode
  valueClassName?: string
}

/** A single header stat — label over a large tabular-numeral value. Used
 * across Dashboard, Activity, Account header rows. */
export function StatTile({ label, value, valueClassName }: StatTileProps) {
  return (
    <div>
      <p className="text-label-md uppercase tracking-wide text-on-surface-variant">{label}</p>
      <p className={`mt-1 text-data-lg ${valueClassName ?? 'text-on-surface'}`}>{value}</p>
    </div>
  )
}
