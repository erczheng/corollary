import { useEffect } from 'react'
import { useUIStore } from '../lib/store'
import { applyTheme } from '../lib/theme'

/** Keeps the `dark` class on <html> in sync with the theme store.
 *
 * The toggling itself lives in `theme.ts`, because `main.tsx` also needs it —
 * it applies the stored theme *before* React mounts, so a dark-theme user
 * does not get a white flash on every reload. This hook covers every change
 * after that first paint. */
export function useThemeSync() {
  const theme = useUIStore((s) => s.theme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])
}
