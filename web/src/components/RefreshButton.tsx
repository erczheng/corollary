import { useEffect, useState } from 'react'

const COOLDOWN_MS = 60_000

/** Rate-limited to one call per 60s, per PRD.md §6.5. */
export function RefreshButton({ onRefresh }: { onRefresh: () => void }) {
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (lastRefreshed === null) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [lastRefreshed])

  const remaining = lastRefreshed === null ? 0 : Math.max(0, COOLDOWN_MS - (now - lastRefreshed))
  const disabled = remaining > 0

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        onRefresh()
        setLastRefreshed(Date.now())
      }}
      className="rounded border border-outline px-3 py-1.5 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low disabled:pointer-events-none disabled:opacity-50"
    >
      {disabled ? `Refresh (${Math.ceil(remaining / 1000)}s)` : 'Refresh'}
    </button>
  )
}
