import { useEffect, useState } from 'react'
import { useUIStore } from '../lib/store'
import { formatTimeET } from '../lib/format'

/** How long without a price before the pill stops claiming to be live.
 *
 * Generous against a 2s tick — this is meant to catch a stream that has
 * actually stopped, not one that skipped a beat. */
export const STALE_AFTER_MS = 15_000

type StreamState = 'connecting' | 'live' | 'stale'

export function streamState(lastTickAt: string | null, now: number): StreamState {
  if (lastTickAt === null) return 'connecting'
  return now - Date.parse(lastTickAt) > STALE_AFTER_MS ? 'stale' : 'live'
}

/** Says whether the page is actually receiving prices.
 *
 * This replaced a Refresh button. A manual refresh on a positions screen
 * is the wrong affordance — the question you have is "are these numbers
 * current", and a button that you press to find out answers it only for
 * the instant after you press it.
 *
 * The `stale` state exists because the alternative is worse than no pill
 * at all: a badge reading "Live" beside a timestamp that stopped moving
 * is a status indicator actively lying about the one thing it is for.
 *
 * **Phase 2 note.** When this is a real socket, reconnecting must not
 * silently resume — CLAUDE.md rule 9 requires an explicit human resume,
 * because reconnecting into an unverified position state is how a bot
 * doubles a position it already holds. Going stale here is a display
 * concern; going *disconnected* there is an engine one, and the two must
 * not be conflated into "it'll come back on its own".
 *
 * A pill, not a control: it reports and does nothing, the same reason the
 * account badge in the header is a pill. */
/** Which feed the pill is reporting on.
 *
 * Both say "Live", because both mean the same thing to whoever is reading:
 * prices are arriving. What differs is the mechanism, and that only shows
 * up in the tooltip — the stream is a 30-symbol websocket scoped to open
 * positions, the poll is a snapshot request across the quoted universe.
 * Labelling one of them "Polled" would suggest the numbers are less current
 * than they are. */
export type FeedKind = 'stream' | 'poll'

export function LiveStatus({ at: feedAt, kind = 'stream' }: { at?: string | null; kind?: FeedKind } = {}) {
  const streamedAt = useUIStore((s) => s.lastTickAt)
  // The prop wins when given. Undefined means "the position stream", which
  // is what every caller predating the Markets poll meant.
  const lastTickAt = feedAt === undefined ? streamedAt : feedAt
  const [now, setNow] = useState(() => Date.now())

  // Staleness is a function of elapsed time, so it needs its own clock —
  // without this the pill only re-evaluates when a price arrives, which is
  // exactly what has stopped happening.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000)
    return () => clearInterval(id)
  }, [])

  const state = streamState(lastTickAt, now)
  const at = lastTickAt ? formatTimeET(lastTickAt) : null

  const label =
    state === 'connecting'
      ? 'Connecting to price stream'
      : state === 'stale'
        ? `Stale — no price since ${at}`
        : `Live — last price ${at}`

  const title =
    state === 'connecting'
      ? kind === 'poll'
        ? 'Waiting for the first snapshot.'
        : 'Waiting for the first price.'
      : state === 'stale'
        ? 'No price has arrived recently. The numbers on this page are not current.'
        : kind === 'poll'
          ? 'Polled snapshots across every quoted symbol. Chains are not streamed — the websocket is capped at 30 symbols and those are spent on open positions.'
          : 'Streaming prices for the open positions in this account.'

  return (
    <span
      // `status`, not `alert`: prices arriving is ordinary, and an
      // assertive live region would interrupt a screen reader every tick.
      role="status"
      aria-label={label}
      title={title}
      className={`flex items-center gap-2 whitespace-nowrap rounded-full border px-3 py-1 text-label-md ${
        state === 'stale'
          ? 'border-caution bg-caution-container text-on-caution-container'
          : 'border-outline-warm text-on-surface-variant'
      }`}
    >
      <span
        className={`h-2 w-2 shrink-0 rounded-full ${
          state === 'live' ? 'animate-pulse bg-bullish' : state === 'stale' ? 'bg-caution' : 'bg-neutral'
        }`}
        aria-hidden="true"
      />
      {state === 'connecting' ? (
        'Connecting…'
      ) : (
        <>
          {state === 'stale' ? 'Stale' : 'Live'} <span className="text-data-md">{at}</span>
        </>
      )}
    </span>
  )
}
