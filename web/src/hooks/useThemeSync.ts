import { useEffect } from 'react'
import { useUIStore } from '../lib/store'

/** Keeps the `dark` class on <html> in sync with the theme store. Every
 * color token resolves through CSS variables scoped to `.dark`, so this
 * one class toggle is the entire theme switch — see src/index.css. */
export function useThemeSync() {
  const theme = useUIStore((s) => s.theme)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
  }, [theme])
}
