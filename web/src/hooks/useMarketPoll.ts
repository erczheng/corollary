import { useEffect } from 'react'
import { useUIStore } from '../lib/store'

/** Drives the market snapshot poll while the Markets page is open.
 *
 * Deliberately a sibling of `useLiveTick` rather than a parameter on it.
 * They stand in for two different Alpaca mechanisms with two different
 * limits: the websocket is capped at 30 symbols on the Basic plan and is
 * spent on open positions, while snapshot requests are bounded by a
 * 200/min budget and can cover a whole page of chains. Phase 2 replaces
 * this one with those requests and the other with the subscription, and
 * collapsing them now would mean pulling them apart then.
 *
 * Pauses when the tab is hidden — a background tab burning polls is wasted
 * work in Phase 1 and wasted request budget in Phase 2. */
export function useMarketPoll(intervalMs: number) {
  const pollMarkets = useUIStore((s) => s.pollMarkets)

  useEffect(() => {
    if (intervalMs <= 0) return

    let id: ReturnType<typeof setInterval> | null = null

    // The interval is passed through so the store scales price movement to
    // the time the poll covers. Without it, raising the rate would raise
    // volatility with it.
    const start = () => {
      if (id === null) id = setInterval(() => pollMarkets(intervalMs), intervalMs)
    }
    const stop = () => {
      if (id !== null) {
        clearInterval(id)
        id = null
      }
    }

    const onVisibility = () => (document.hidden ? stop() : start())

    // One snapshot immediately, then on the interval. A page that showed
    // skeletons for two seconds on every visit would be reporting a
    // connection problem it does not have.
    if (!document.hidden) {
      pollMarkets(intervalMs)
      start()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [pollMarkets, intervalMs])
}
