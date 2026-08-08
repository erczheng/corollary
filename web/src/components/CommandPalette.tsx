import { useEffect, useRef } from 'react'
import { useUIStore } from '../lib/store'

/** Ctrl+K palette. Stub for Phase 1 — no commands are wired up yet. */
export function CommandPalette() {
  const open = useUIStore((s) => s.paletteOpen)
  const close = useUIStore((s) => s.closePalette)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  useEffect(() => {
    if (!open) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, close])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-32"
      role="presentation"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="w-full max-w-lg rounded-lg border border-outline-warm bg-surface-container-lowest/20 p-3 shadow-hover dark:shadow-none"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          ref={inputRef}
          type="text"
          placeholder="Type a command…"
          className="w-full rounded border border-outline bg-surface px-4 py-2 text-body-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
        />
        <p className="mt-3 px-1 text-caption text-on-surface-variant">
          Coming soon — jump to a page, execute a recommendation, or halt trading.
        </p>
      </div>
    </div>
  )
}
