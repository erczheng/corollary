import { useEffect, useState } from 'react'
import { RefreshIcon } from './icons'

const COOLDOWN_MS = 60_000
const SPIN_MS = 600

/** Rate-limited to one call per 60s, per PRD.md §6.5.
 *
 * The wheel spins for a fixed beat because Phase 1 refreshes mock data
 * synchronously and there is nothing to wait on. In Phase 2 this should
 * be driven by the query's own `isFetching` instead of a timer, so the
 * spin reflects real work rather than standing in for it. */
export function RefreshButton({ onRefresh }: { onRefresh: () => void }) {
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null)
  const [spinning, setSpinning] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (lastRefreshed === null) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [lastRefreshed])

  useEffect(() => {
    if (!spinning) return
    const id = setTimeout(() => setSpinning(false), SPIN_MS)
    return () => clearTimeout(id)
  }, [spinning])

  const remaining = lastRefreshed === null ? 0 : Math.max(0, COOLDOWN_MS - (now - lastRefreshed))
  const disabled = remaining > 0
  const seconds = Math.ceil(remaining / 1000)
  const label = disabled
    ? `Refresh rate-limited — available in ${seconds}s`
    : 'Refresh recommendations'

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        onRefresh()
        setLastRefreshed(Date.now())
        setSpinning(true)
      }}
      aria-label={label}
      title={label}
      className="flex h-8 w-8 items-center justify-center rounded-full text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low hover:text-on-surface disabled:pointer-events-none disabled:opacity-40"
    >
      <RefreshIcon className={`h-4 w-4 ${spinning ? 'animate-spin' : ''}`} />
    </button>
  )
}
