import { useState } from 'react'
import { ConfirmDialog } from './ConfirmDialog'
import { RequestFailed } from './RequestFailed'
import { TableSkeleton } from './Skeleton'
import { ORDER_TYPE_LABEL, TIME_IN_FORCE_LABEL, type WorkingOrder } from '../lib/types'
import { ORDER_SIDE_LABEL } from '../lib/orders'
import { formatDateTimeET, formatUsd } from '../lib/format'

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'
/** 32px rows — see DESIGN.md "Tables". This table carries a Cancel button,
 * so without the height it would sit 4px taller than a table that
 * doesn't. */
const TR = 'h-8 border-t border-outline/10 hover:bg-surface-container-low'
/** A row action: 24px tall, so it clears the 24px minimum target size and
 * still leaves 4px either side of it in a 32px row. `flex w-fit` rather
 * than the inline default because a lone inline-level control in a cell
 * sits on the cell's text baseline and drags a descender's worth of strut
 * in with it — 25.5px around a 24px button, which is what made this row
 * 1px taller than the same row on Activity. `ml-auto` right-aligns it
 * without an extra wrapper. */
const ROW_BUTTON =
  'ml-auto flex h-6 w-fit items-center rounded border border-outline px-2 text-label-sm text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low hover:text-on-surface'

/** The price the order is actually working at. A stop-limit has both a
 * trigger and a limit and they are not interchangeable, so both are shown
 * rather than picking one and hoping. */
function priceLabel(order: WorkingOrder): string {
  if (order.orderType === 'stop_limit' && order.stopPrice !== null && order.limitPrice !== null) {
    return `${formatUsd(order.stopPrice)} → ${formatUsd(order.limitPrice)}`
  }
  if (order.orderType === 'stop' && order.stopPrice !== null) return formatUsd(order.stopPrice)
  if (order.limitPrice !== null) return formatUsd(order.limitPrice)
  return '—'
}

interface WorkingOrdersProps {
  orders: readonly WorkingOrder[]
  loading: boolean
  accountLabel: string
  /** A failed request, as opposed to a book with no working orders. An
   * empty table where the fetch failed would claim there is nothing
   * resting at the broker, which is the one thing it cannot know. */
  error?: unknown
  /** Cancel a resting order. **Absent means there is no way to cancel**,
   * and the control renders disabled with `cancelUnavailableReason` stated
   * rather than silently doing nothing. Phase 2 is read-only — a cancel
   * would be the first broker write in the codebase and would land before
   * the risk manager exists — so nothing passes this yet. */
  onCancel?: (order: WorkingOrder) => void
  cancelUnavailableReason?: string
}

/** Orders that are placed and haven't filled.
 *
 * Attached exits are deliberately absent: they live on their position,
 * which is where they are edited and cancelled. Listing them here as well
 * would give one thing two homes that could disagree about it.
 *
 * Takes its orders as a prop rather than reading the store: they come from
 * `GET /api/positions/working` now, and a component that read the store
 * would render the Phase 1 fixtures beside live positions with nothing on
 * screen to say the two came from different places. */
export function WorkingOrders({
  orders,
  loading,
  accountLabel,
  error,
  onCancel,
  cancelUnavailableReason,
}: WorkingOrdersProps) {
  const [cancelTarget, setCancelTarget] = useState<WorkingOrder | null>(null)
  const canCancel = onCancel !== undefined

  return (
    <section className="mt-8 rounded-lg border border-outline-warm bg-surface-container-lowest">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
        <h2 className="text-title-lg text-on-surface">Working Orders</h2>
        <span className="text-label-md text-on-surface-variant">
          {loading ? 'Loading…' : error ? '' : `${orders.length} working in ${accountLabel}`}
        </span>
      </div>

      {loading ? (
        <TableSkeleton rows={2} columns={6} label="Loading working orders" />
      ) : error ? (
        <div className="px-4 py-6">
          <RequestFailed error={error} what="working orders" />
        </div>
      ) : orders.length === 0 ? (
        <p className="px-4 py-6 text-body-md text-on-surface-variant">
          No working orders. A limit or stop order appears here until it fills or you cancel it — a market
          order fills immediately and goes straight to the ledger below. Exits attached to a position live
          on that position, in Open Positions.
        </p>
      ) : (
        <table className="w-full border-collapse">
          <thead>
            <tr className="bg-surface-container">
              <th className={`${TH} text-left`}>Placed</th>
              <th className={`${TH} w-full text-left`}>Asset</th>
              <th className={`${TH} text-left`}>Side</th>
              <th className={`${TH} text-left`}>Type</th>
              <th className={`${TH} text-right`}>Price</th>
              <th className={`${TH} text-right`}>Qty</th>
              <th className={`${TH} text-right`}>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id} className={TR}>
                <td className={`${TD} whitespace-nowrap text-caption text-on-surface-variant`}>
                  {formatDateTimeET(o.placedAt)}
                </td>
                <td className={`${TD} max-w-0 text-body-sm text-on-surface`}>
                  <span className="block truncate" title={o.contract}>
                    {o.contract}
                  </span>
                </td>
                <td className={`${TD} whitespace-nowrap text-label-md text-on-surface`}>{o.side}</td>
                <td className={`${TD} whitespace-nowrap text-label-md text-on-surface`}>
                  {ORDER_TYPE_LABEL[o.orderType]}
                  <span className="ml-1 text-caption text-on-surface-variant">
                    {TIME_IN_FORCE_LABEL[o.timeInForce]}
                  </span>
                </td>
                <td className={`${TD} whitespace-nowrap text-right text-data-md text-on-surface`}>
                  {priceLabel(o)}
                </td>
                <td className={`${TD} text-right text-data-md text-on-surface`}>{o.quantity}</td>
                <td className={`${TD} text-right`}>
                  <button
                    type="button"
                    disabled={!canCancel}
                    onClick={() => setCancelTarget(o)}
                    title={
                      canCancel
                        ? `Cancel ${o.side} ${o.contract}`
                        : cancelUnavailableReason
                    }
                    className={`${ROW_BUTTON}${canCancel ? '' : ' cursor-not-allowed opacity-40'}`}
                  >
                    Cancel
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!canCancel && !loading && !error && orders.length > 0 && cancelUnavailableReason && (
        <p className="border-t border-outline-variant px-4 py-3 text-caption text-on-surface-variant">
          {cancelUnavailableReason}
        </p>
      )}

      <ConfirmDialog
        open={cancelTarget !== null}
        title="Cancel this order?"
        confirmLabel="Cancel order"
        consequence={
          cancelTarget && (
            <>
              Cancels the {ORDER_TYPE_LABEL[cancelTarget.orderType].toLowerCase()} order to{' '}
              {ORDER_SIDE_LABEL[cancelTarget.side].toLowerCase()} {cancelTarget.quantity} ×{' '}
              {cancelTarget.contract} at {priceLabel(cancelTarget)}. The position itself is untouched and
              stays open — cancelling an order is not closing a trade.
            </>
          )
        }
        onConfirm={() => {
          if (cancelTarget && onCancel) onCancel(cancelTarget)
          setCancelTarget(null)
        }}
        onCancel={() => setCancelTarget(null)}
      />
    </section>
  )
}
