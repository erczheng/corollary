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

/** A single header stat — label over a large tabular-numeral value, with no
 * card chrome around it.
 *
 * **Currently unrendered.** Activity used this until its header stats moved
 * to three `StatCard`s, which carry the same label/value/note but add the
 * border, surface and icon the Dashboard's cards have. StatCard now
 * subsumes this entirely — the only thing StatTile still offers is the
 * chrome-less version, for a stat that sits inside a panel rather than
 * being one. If the Account page hasn't wanted that by the time real data
 * lands, delete it. */
export function StatTile({ label, value, valueClassName, note }: StatTileProps) {
  return (
    <div>
      <p className="text-label-md uppercase tracking-wide text-on-surface-variant">{label}</p>
      <p className={`mt-1 text-data-lg ${valueClassName ?? 'text-on-surface'}`}>{value}</p>
      {note && <p className="mt-1 text-caption text-on-surface-variant">{note}</p>}
    </div>
  )
}
