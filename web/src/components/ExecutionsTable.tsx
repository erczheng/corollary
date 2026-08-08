import {
  ACTIVITY_ACTION_LABEL,
  ACTIVITY_STATUS_LABEL,
  type ActivityItem,
  type ActivityStatus,
} from '../lib/mockData'
import { formatDateTimeET, formatUsd, signClass } from '../lib/format'

/** One table, two pages. The Dashboard's "Recent Executions" and Activity's
 * "Recent Activity" are the same feed viewed at different depths — the
 * Dashboard shows the most recent handful under a scroll cap, Activity
 * paginates the whole thing. They render from this component so the columns,
 * the alignment, and the P&L-vs-status logic cannot drift apart. */

export const ACTIVITY_FILTERS: (ActivityStatus | 'all')[] = [
  'all',
  'filled',
  'rejected',
  'pending',
  'canceled',
]

export type ActivityFilter = (typeof ACTIVITY_FILTERS)[number]

export function StatusFilterSelect({
  value,
  onChange,
  label,
}: {
  value: ActivityFilter
  onChange: (value: ActivityFilter) => void
  label: string
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value as ActivityFilter)}
      aria-label={label}
      className="rounded border border-outline bg-surface px-2 py-2 text-label-md text-on-surface focus:border-primary"
    >
      {ACTIVITY_FILTERS.map((f) => (
        <option key={f} value={f}>
          {f === 'all' ? 'All statuses' : ACTIVITY_STATUS_LABEL[f]}
        </option>
      ))}
    </select>
  )
}

/** The export carries the fields the table folds together — price and the
 * rejection reason included. A CSV is for reconciling against the broker,
 * so it should lose less than the screen does, not the same amount. */
export function activityCsvRows(items: ActivityItem[]): Record<string, string | number>[] {
  return items.map((a) => ({
    time: a.time,
    contract: a.contract,
    action: a.action,
    price: a.price ?? '',
    quantity: a.quantity ?? '',
    pnl: a.pnl ?? '',
    pnl_pct: a.pnlPct ?? '',
    amount: a.amount ?? '',
    status: a.status,
    rejection_reason: a.rejectionReason ?? '',
  }))
}

interface ExecutionsTableProps {
  items: ActivityItem[]
  /** Activity spells the rejection reason out inline; the Dashboard leaves
   * it on hover. PRD.md §8.2 makes Activity the page of record for
   * rejections, and a reason reachable only by hovering is a reason that
   * doesn't exist on a screenshot, on a touch device, or to a keyboard. */
  showRejectionReason?: boolean
  /** Stretch the rows to fill the height available. The Dashboard sits this
   * table beside Recommended Trades and the two panels have to end level;
   * table rows distribute slack for free, where a div list would not. Off
   * on Activity, where the table is in normal flow and stretching rows
   * would just space a paginated list oddly. */
  fill?: boolean
}

export function ExecutionsTable({
  items,
  showRejectionReason = false,
  fill = false,
}: ExecutionsTableProps) {
  return (
    <table className={`w-full border-collapse ${fill ? 'h-full' : ''}`}>
      <thead>
        {/* Sticky so the columns stay identifiable while the body scrolls
            inside the panel. Harmless where the table is paginated
            instead. */}
        <tr className="sticky top-0 bg-surface-container">
          <th className="whitespace-nowrap px-3 py-2 text-left text-label-md uppercase text-on-surface-variant">
            Time
          </th>
          {/* w-full lets this column absorb the table's slack so the
              contract truncates as late as possible; it's the column
              carrying the most information. */}
          <th className="w-full px-3 py-2 text-left text-label-md uppercase text-on-surface-variant">
            Asset / Action
          </th>
          <th className="px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">Qty</th>
          {/* Fill price per contract. It was only in the CSV before, which
              meant reconciling a fill against the broker took an export.
              PRD.md §8.2 lists it as an Activity column. */}
          <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
            Price
          </th>
          <th className="whitespace-nowrap px-3 py-2 text-right text-label-md uppercase text-on-surface-variant">
            P&amp;L
          </th>
        </tr>
      </thead>
      <tbody>
        {items.map((a) => (
          <tr key={a.id} className="border-t border-outline/10 hover:bg-surface-container-low">
            <td className="whitespace-nowrap px-3 py-2 align-top text-caption text-on-surface-variant">
              {formatDateTimeET(a.time)}
            </td>
            <td
              className="max-w-0 px-3 py-2 align-top text-body-md text-on-surface"
              title={a.contract === '—' ? undefined : a.contract}
            >
              <span className="block truncate">
                {a.contract === '—'
                  ? ACTIVITY_ACTION_LABEL[a.action]
                  : `${ACTIVITY_ACTION_LABEL[a.action]} ${a.contract}`}
              </span>
              {showRejectionReason && a.rejectionReason && (
                /* `error`, not `bearish` — a rejection is a rule outcome,
                   not a losing position (CLAUDE.md, DESIGN.md). */
                <span className="mt-1 block text-caption text-error">{a.rejectionReason}</span>
              )}
            </td>
            <td className="px-3 py-2 align-top text-right text-data-md text-on-surface">{a.quantity ?? '—'}</td>
            {/* A cash movement has no fill price, and a canceled order never
                got one. Em dash rather than a zero, which would read as a
                free fill. */}
            <td className="px-3 py-2 align-top text-right text-data-md text-on-surface">
              {a.price !== null ? formatUsd(a.price) : '—'}
            </td>
            {/* P&L, and only P&L. This column used to fall back to the status
                word when a row had none, which meant its header said one thing
                and most of its cells showed another.

                The cost of the split is real and worth stating: an em dash here
                covers an opening fill, a pending order, a cancel and a
                rejection alike, so those four no longer look different at a
                glance. `title` keeps the status reachable on hover, but hover
                is not a substitute — it's absent from a screenshot, a touch
                device and a keyboard. Activity is the page that still spells
                rejections out, inline and in `error`, via showRejectionReason
                (PRD.md §8.2 makes it the page of record for them). */}
            <td
              className="whitespace-nowrap px-3 py-2 align-top text-right"
              title={
                a.rejectionReason
                  ? `${ACTIVITY_STATUS_LABEL[a.status]} — ${a.rejectionReason}`
                  : ACTIVITY_STATUS_LABEL[a.status]
              }
            >
              {a.pnl !== null ? (
                <span className={`text-data-md ${signClass(a.pnl)}`}>{formatUsd(a.pnl, { signed: true })}</span>
              ) : a.amount !== null ? (
                /* Deliberately not signClass: a deposit is money you moved,
                   not money the account made, and rendering it bullish green
                   would read as a gain. The sign still carries direction. */
                <span className="text-data-md text-on-surface">
                  {formatUsd(a.amount, { signed: true })}
                </span>
              ) : (
                <span className="text-data-md text-on-surface-variant">—</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
