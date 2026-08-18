import { useEffect } from 'react'
import { useUIStore } from '../lib/store'

/** Drives the news poll while the News page is open.
 *
 * A sibling of `useLiveTick` and `useMarketPoll` rather than a parameter on
 * either, for the same reason those two are separate from each other: they
 * stand in for three different Alpaca mechanisms. The websocket is capped
 * at 30 symbols and spent on open positions, snapshot requests are bounded
 * by a request budget and cover the chains, and the news endpoint is
 * neither — it is not per-symbol at all, and its cadence is set by how
 * often headlines are actually published rather than by how fast a price
 * moves.
 *
 * Pauses when the tab is hidden. A background tab burning polls is wasted
 * work in Phase 1 and wasted request budget in Phase 2. */
export function useNewsPoll(intervalMs: number) {
  const pollNews = useUIStore((s) => s.pollNews)

  useEffect(() => {
    if (intervalMs <= 0) return

    let id: ReturnType<typeof setInterval> | null = null

    const start = () => {
      if (id === null) id = setInterval(() => pollNews(), intervalMs)
    }
    const stop = () => {
      if (id !== null) {
        clearInterval(id)
        id = null
      }
    }

    const onVisibility = () => (document.hidden ? stop() : start())

    // One poll immediately, then on the interval. The corpus is already
    // loaded, so this is what takes the feed out of its loading state — a
    // page that showed skeletons over data it already had would be
    // reporting a connection problem it does not have.
    if (!document.hidden) {
      pollNews()
      start()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [pollNews, intervalMs])
}
