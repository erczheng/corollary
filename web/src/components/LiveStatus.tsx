import { useUIStore } from '../lib/store'
import { formatTimeET } from '../lib/format'

/** Says whether the page is actually receiving prices, rather than
 * claiming to be live and leaving you to guess.
 *
 * This replaced a Refresh button. A manual refresh on a positions screen
 * is the wrong affordance — the question you have is "are these numbers
 * current", and a button that you have to press to find out answers it
 * only for the instant after you press it.
 *
 * A pill, not a control: it reports and does nothing, which is the same
 * reason the account badge in the header is a pill. */
export function LiveStatus() {
  const lastTickAt = useUIStore((s) => s.lastTickAt)
  const live = lastTickAt !== null

  return (
    <span
      // `status`, not `alert`: prices arriving is ordinary, and an
      // assertive live region would interrupt a screen reader every tick.
      role="status"
      aria-label={live ? `Live — last price ${formatTimeET(lastTickAt)}` : 'Connecting to price stream'}
      title={
        live
          ? 'Streaming prices for the open positions in this account.'
          : 'Waiting for the first price.'
      }
      className="flex items-center gap-2 whitespace-nowrap rounded-full border border-outline-warm px-3 py-1 text-label-md text-on-surface-variant"
    >
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${live ? 'animate-pulse bg-bullish' : 'bg-neutral'}`}
        aria-hidden="true"
      />
      {live ? (
        <>
          Live <span className="text-data-md text-on-surface">{formatTimeET(lastTickAt)}</span>
        </>
      ) : (
        'Connecting…'
      )}
    </span>
  )
}
