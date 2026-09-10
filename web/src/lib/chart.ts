import type { PricePoint } from './types'

export interface RangeChange {
  from: PricePoint
  to: PricePoint
  /** Absolute change in account currency over the window. */
  change: number
  /** Change as a percentage of the value at the start of the window. */
  changePct: number
}

/** The change between two points of a series, for the chart's
 * drag-to-measure readout.
 *
 * Indices arrive in whichever order the drag happened — dragging right to
 * left is the same window as left to right — so they're normalized here
 * rather than at every call site. A drag that never left its starting
 * point is not a window and returns null, so a stray click doesn't flash a
 * "+$0.00" reading that looks like a measurement.
 */
export function rangeChange(
  points: readonly PricePoint[],
  a: number,
  b: number,
): RangeChange | null {
  const lo = Math.min(a, b)
  const hi = Math.max(a, b)
  if (lo === hi) return null

  const from = points[lo]
  const to = points[hi]
  // A zero starting value would make the percentage meaningless rather
  // than merely large, so the whole reading is withheld.
  if (!from || !to || from.value === 0) return null

  const change = to.value - from.value
  return { from, to, change, changePct: (change / from.value) * 100 }
}
