/** Theme persistence.
 *
 * The one piece of UI state that survives a reload, and the boundary is
 * deliberate: **nothing else in this app may be persisted this way.** PRD.md
 * §2 and CLAUDE.md rule 5 require every cold start to come up in Paper, so a
 * general "save the store to localStorage" helper is precisely the change
 * that would restore Cash mode without a confirm. This module knows about one
 * key and one value, so it cannot grow into that by accident.
 *
 * A theme is also the only preference here where the wrong answer costs
 * nothing. Getting the account wrong costs money.
 */

export type Theme = 'light' | 'dark'

const KEY = 'corollary:theme'

/** What a first run gets, and what a corrupt value falls back to.
 *
 * Not `prefers-color-scheme`: that would change the theme this app opens
 * with today, which is a bigger decision than "remember what I picked". */
const DEFAULT: Theme = 'light'

function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark'
}

/** Reads the stored preference, falling back to the default.
 *
 * Every access is guarded. `localStorage` is not merely absent in some
 * environments — reading it *throws* in others (Safari private browsing,
 * storage disabled by policy, some embedded webviews). An unreadable
 * preference must never stop the terminal from starting, so the failure mode
 * is "the default theme", not a blank page.
 *
 * A stored value that is not one of the two themes is discarded rather than
 * trusted: it can only come from a hand-edited or stale key, and passing it
 * through would put an unknown string into the `dark` class toggle. */
export function loadTheme(): Theme {
  try {
    const stored = window.localStorage.getItem(KEY)
    return isTheme(stored) ? stored : DEFAULT
  } catch {
    return DEFAULT
  }
}

/** Records the preference. Silently does nothing if storage is unavailable —
 * the theme still applies for this session, it just will not be remembered,
 * which is a better outcome than a toggle that throws when clicked. */
export function saveTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(KEY, theme)
  } catch {
    // Storage unavailable. The session keeps the theme; the next one won't.
  }
}

/** Puts the theme on `<html>`.
 *
 * Every colour token resolves through CSS variables scoped to `.dark`, so
 * this one class toggle is the whole theme switch — see `index.css`.
 *
 * Called twice by design: once from `main.tsx` *before* React mounts, and
 * thereafter from `useThemeSync` whenever the store changes. The first call is
 * what stops a dark-theme user seeing a white page on every reload — the
 * store's initial value is correct immediately, but an effect does not run
 * until after the first paint, and that paint would be light. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
}
