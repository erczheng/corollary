/** Loading placeholders.
 *
 * These are designed rather than improvised because CLAUDE.md asks for it,
 * and because the alternative — a blank panel, or worse a panel that says
 * "no positions" — makes an account that is merely slow look like an
 * account that is empty. Those two mean very different things at 9:31am.
 *
 * The bars carry `aria-hidden` and the region carries the announcement, so
 * a screen reader hears "Loading positions" once instead of a stack of
 * meaningless boxes.
 */

function Bar({ className = '' }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`block h-3 animate-pulse rounded bg-surface-container-high ${className}`}
    />
  )
}

/** A single figure that hasn't arrived yet, for standing in place of one
 * number rather than a whole panel.
 *
 * The Account page's equity reconciliation needs this: cash is a static
 * balance from the broker and arrives immediately, while position value is
 * marked from the price stream and does not. Showing cost basis in the
 * meantime would state an equity figure that is wrong by the whole
 * unrealized P&L, and showing a zero would read as a flat book. */
export function ValueSkeleton({ label, className = 'w-24' }: { label: string; className?: string }) {
  return (
    <span role="status" aria-live="polite" aria-busy="true" className="inline-block align-middle">
      <span className="sr-only">{label}</span>
      <Bar className={`h-4 ${className}`} />
    </span>
  )
}

export function TableSkeleton({ rows, columns, label }: { rows: number; columns: number; label: string }) {
  return (
    <div role="status" aria-live="polite" aria-busy="true">
      <span className="sr-only">{label}</span>
      <table className="w-full border-collapse">
        <tbody>
          {Array.from({ length: rows }, (_, r) => (
            <tr key={r} className="border-t border-outline/10">
              {Array.from({ length: columns }, (_, c) => (
                <td key={c} className="px-3 py-3">
                  {/* The first column is the wide one on every table this
                      stands in for, so the bars echo the real layout
                      rather than making a uniform grid the content will
                      not match. */}
                  <Bar className={c === 0 ? 'w-3/4' : 'ml-auto w-12'} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function StatCardSkeleton({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <span className="sr-only">{label}</span>
      <Bar className="w-24" />
      <Bar className="mt-3 h-6 w-32" />
      <Bar className="mt-2 w-40" />
    </div>
  )
}
