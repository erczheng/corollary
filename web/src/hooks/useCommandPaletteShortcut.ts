import { useEffect } from 'react'
import { useUIStore } from '../lib/store'

/** Ctrl+K (Cmd+K on macOS) opens the command palette from anywhere. */
export function useCommandPaletteShortcut() {
  const openPalette = useUIStore((s) => s.openPalette)

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        openPalette()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [openPalette])
}
