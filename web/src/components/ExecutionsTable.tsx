import {
  ACTIVITY_ACTION_LABEL,
  ACTIVITY_STATUS_CLASS,
  ACTIVITY_STATUS_LABEL,
  type ActivityItem,
  type ActivityStatus,
} from '../lib/mockData'
import { formatDateTimeET, formatUsd, signClass } from '../lib/format'

/** One table, two pages, two column sets. The Dashboard's "Recent
 * Executions" and Activity's "Recent Activity" are the same feed viewed at
 * different depths, so they render from this component: the cell logic —
 * what counts as P&L, when a price is unknowable, which colour a status
 * takes — is written once and cannot drift between the pages.
 *
 * What they don't share is width. The Dashboard sits its table in a
 * half-width panel beside Recommended Trades and shows a summary; Activity
 * has the full page and is the ledger of record, so it splits Asset from
 * Action and gives Status its own column. */

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

/** The export carries the fields no layout shows — P&L percent and the
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

export type ExecutionsLayout = 'summary' | 'full'

type ColumnKey = 'time' | 'asset' | 'action' | 'assetAction' | 'price' | 'qty' | 'pnl' | 'status'

/** The two column sets, written out rather than assembled from conditions.
 * They differ in more than presence — the summary puts Qty before Price and
 * ends on P&L, the full ledger leads with P&L and ends on Status — and a
 * pile of inline ternaries made it far too easy to change one layout while
 * meaning to change the other. */
const COLUMN_LAYOUTS: Record<ExecutionsLayout, ColumnKey[]> = {
  summary: ['time', 'assetAction', 'qty', 'price', 'pnl'],
  full: ['time', 'asset', 'action', 'pnl', 'price', 'qty', 'status'],
}

const COLUMNS: Record<ColumnKey, { label: string; align: 'left' | 'right'; grow?: boolean }> = {
  time: { label: 'Time', align: 'left' },
  // `grow` absorbs the table's slack so the contract truncates as late as
  // possible; it's the column carrying the most information.
  asset: { label: 'Asset', align: 'left', grow: true },
  assetAction: { label: 'Asset / Action', align: 'left', grow: true },
  action: { label: 'Action', align: 'left' },
  price: { label: 'Price', align: 'right' },
  qty: { label: 'Qty', align: 'right' },
  pnl: { label: 'P&L', align: 'right' },
  status: { label: 'Status', align: 'right' },
}

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'
/** Every table row in the app is 32px: `h-8` here, `px-3 py-1` on the
 * cells. The height is set on the row rather than by the padding because a
 * row action is 24px tall and a line of row text is 20px — left to the
 * padding alone the two would land 4px apart, which is what made a table
 * with buttons taller than a table without one. See DESIGN.md "Tables". */
const TR = 'h-8 border-t border-outline/10 hover:bg-surface-container-low'
const TD_NUM = `${TD} text-right text-data-md text-on-surface`

/** P&L if the trade produced one, the cash moved if it was a deposit or a
 * withdrawal, otherwise an em dash. An opening fill has realized nothing
 * yet, and a rejection never will. */
function PnlCell({ item }: { item: ActivityItem }) {
  if (item.pnl !== null) {
    return <span className={`text-data-md ${signClass(item.pnl)}`}>{formatUsd(item.pnl, { signed: true })}</span>
  }
  if (item.amount !== null) {
    /* Deliberately not signClass: a deposit is money you moved, not money
       the account made, and rendering it bullish green would read as a
       gain. The sign still carries direction. */
    return <span className="text-data-md text-on-surface">{formatUsd(item.amount, { signed: true })}</span>
  }
  return <span className="text-data-md text-on-surface-variant">—</span>
}

/** A cash movement has no fill price, and a canceled order never got one.
 * Em dash rather than a zero, which would read as a free fill. */
function priceText(item: ActivityItem): string {
  return item.price !== null ? formatUsd(item.price) : '—'
}

function RejectionReason({ item }: { item: ActivityItem }) {
  if (!item.rejectionReason) return null
  /* `error`, not `bearish` — a rejection is a rule outcome, not a losing
     position (CLAUDE.md, DESIGN.md). */
  return <span className="mt-1 block text-caption text-error">{item.rejectionReason}</span>
}

function Cell({
  column,
  item,
  showRejectionReason,
  statusOnHover,
}: {
  column: ColumnKey
  item: ActivityItem
  showRejectionReason: boolean
  statusOnHover: boolean
}) {
  switch (column) {
    case 'time':
      return (
        <td className={`${TD} whitespace-nowrap text-caption text-on-surface-variant`}>
          {formatDateTimeET(item.time)}
        </td>
      )

    case 'asset':
    case 'assetAction': {
      // Split, the asset column holds the contract alone. Merged, a cash
      // movement has no contract to pair with, so the action fills the cell
      // rather than leaving it blank.
      const cashMovement = item.contract === '—'
      const text =
        column === 'asset'
          ? cashMovement
            ? '—'
            : item.contract
          : cashMovement
            ? ACTIVITY_ACTION_LABEL[item.action]
            : `${ACTIVITY_ACTION_LABEL[item.action]} ${item.contract}`
      return (
        <td
          className={`${TD} max-w-0 text-body-sm text-on-surface`}
          title={cashMovement ? undefined : item.contract}
        >
          <span className="block truncate">{text}</span>
          {showRejectionReason && <RejectionReason item={item} />}
        </td>
      )
    }

    case 'action':
      /* BTO/STC/STO/BTC are the jargon, not shorthand to expand. Label
         type, not data type — these are words, not quantities, so they
         don't want tabular figures. */
      return (
        <td className={`${TD} whitespace-nowrap text-label-md text-on-surface`}>
          {ACTIVITY_ACTION_LABEL[item.action]}
        </td>
      )

    case 'price':
      return <td className={TD_NUM}>{priceText(item)}</td>

    case 'qty':
      return <td className={TD_NUM}>{item.quantity ?? '—'}</td>

    case 'pnl':
      return (
        <td
          className={`${TD} whitespace-nowrap text-right`}
          /* Where the layout has no status column, the status lives on
             hover here. That is a real loss — an em dash covers an opening
             fill, a pending order, a cancel and a rejection alike, and
             hover is absent from a screenshot, a touch device and a
             keyboard. It's the price of a half-width panel; Activity shows
             the column outright. */
          title={
            statusOnHover
              ? item.rejectionReason
                ? `${ACTIVITY_STATUS_LABEL[item.status]} — ${item.rejectionReason}`
                : ACTIVITY_STATUS_LABEL[item.status]
              : undefined
          }
        >
          <PnlCell item={item} />
        </td>
      )

    case 'status':
      return (
        <td className={`${TD} whitespace-nowrap text-right`}>
          <span className={`text-caption ${ACTIVITY_STATUS_CLASS[item.status]}`}>
            {ACTIVITY_STATUS_LABEL[item.status]}
          </span>
        </td>
      )
  }
}

interface ExecutionsTableProps {
  items: ActivityItem[]
  /** `summary` — Dashboard: Time, Asset / Action, Qty, Price, P&L.
   *  `full` — Activity: Time, Asset, Action, P&L, Price, Qty, Status. */
  layout?: ExecutionsLayout
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
  layout = 'summary',
  showRejectionReason = false,
  fill = false,
}: ExecutionsTableProps) {
  const columns = COLUMN_LAYOUTS[layout]
  const statusOnHover = !columns.includes('status')

  return (
    <table className={`w-full border-collapse ${fill ? 'h-full' : ''}`}>
      <thead>
        {/* Sticky so the columns stay identifiable while the body scrolls
            inside the panel. Harmless where the table is paginated
            instead. */}
        <tr className="sticky top-0 bg-surface-container">
          {columns.map((key) => {
            const col = COLUMNS[key]
            return (
              <th
                key={key}
                className={`${TH} ${col.align === 'right' ? 'text-right' : 'text-left'} ${
                  col.grow ? 'w-full' : ''
                }`}
              >
                {col.label}
              </th>
            )
          })}
        </tr>
      </thead>
      <tbody>
        {items.map((a) => (
          <tr key={a.id} className={TR}>
            {columns.map((key) => (
              <Cell
                key={key}
                column={key}
                item={a}
                showRejectionReason={showRejectionReason}
                statusOnHover={statusOnHover}
              />
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
