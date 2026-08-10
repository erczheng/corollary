import { useEffect } from 'react'
import { useUIStore } from '../lib/store'

/** Drives the price stream while the page is open.
 *
 * Stands in for the Alpaca WebSocket. Phase 2 replaces the interval with
 * the real subscription and everything downstream — re-marking, fills,
 * exit triggers — stays as it is, because the store already treats a tick
 * as "a price arrived" rather than "a timer fired".
 *
 * Pauses when the tab is hidden. A background tab burning ticks is wasted
 * work in Phase 1 and wasted stream quota in Phase 2, where CLAUDE.md caps
 * the Basic plan at 30 symbols.
 *
 * Note for Phase 2: reconnecting must not silently resume. CLAUDE.md rule
 * 9 requires an explicit human resume after a dropped connection, because
 * reconnecting into an unverified position state is how a bot doubles a
 * position it already holds. This hook has no such state to lose yet — but
 * whatever replaces it must. */
export function useLiveTick(intervalMs: number) {
  const tick = useUIStore((s) => s.tick)

  useEffect(() => {
    if (intervalMs <= 0) return

    let id: ReturnType<typeof setInterval> | null = null

    // The interval is passed through so the store scales price movement to
    // the time the tick covers. Without it, raising the rate would raise
    // volatility with it.
    const start = () => {
      if (id === null) id = setInterval(() => tick(intervalMs), intervalMs)
    }
    const stop = () => {
      if (id !== null) {
        clearInterval(id)
        id = null
      }
    }

    const onVisibility = () => (document.hidden ? stop() : start())

    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [tick, intervalMs])
}
