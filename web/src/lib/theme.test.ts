import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { applyTheme, loadTheme, saveTheme } from './theme'

const KEY = 'corollary:theme'

beforeEach(() => {
  window.localStorage.clear()
  document.documentElement.classList.remove('dark')
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('loadTheme', () => {
  it('returns the stored preference', () => {
    window.localStorage.setItem(KEY, 'dark')
    expect(loadTheme()).toBe('dark')

    window.localStorage.setItem(KEY, 'light')
    expect(loadTheme()).toBe('light')
  })

  it('falls back to light on a first run', () => {
    expect(loadTheme()).toBe('light')
  })

  /** Can only come from a hand-edited or stale key, and passing it through
   * would put an unknown string into the `dark` class toggle. */
  it('discards a value that is not a theme', () => {
    window.localStorage.setItem(KEY, 'midnight')
    expect(loadTheme()).toBe('light')
  })

  /** Reading storage does not merely return null in some environments, it
   * *throws* — Safari private browsing, storage disabled by policy. An
   * unreadable preference must never stop the terminal from starting. */
  it('survives storage that throws on read', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })

    expect(() => loadTheme()).not.toThrow()
    expect(loadTheme()).toBe('light')
  })
})

describe('saveTheme', () => {
  it('records the preference so the next load returns it', () => {
    saveTheme('dark')
    expect(loadTheme()).toBe('dark')
  })

  /** The session keeps the theme; the next one won't. Better than a toggle
   * that throws when clicked. */
  it('survives storage that throws on write', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })

    expect(() => saveTheme('dark')).not.toThrow()
  })
})

describe('applyTheme', () => {
  it('puts the dark class on <html> and takes it off again', () => {
    applyTheme('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    applyTheme('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('is idempotent', () => {
    applyTheme('dark')
    applyTheme('dark')
    expect(document.documentElement.className.match(/dark/g)).toHaveLength(1)
  })
})

/** The boundary this module exists to hold. PRD.md §2 and CLAUDE.md rule 5
 * require every cold start to come up in Paper, so the thing that must never
 * happen is this file growing into a general store-persistence helper. */
describe('what is persisted', () => {
  it('writes exactly one key, and it is the theme', () => {
    saveTheme('dark')

    expect(Object.keys(window.localStorage)).toEqual([KEY])
    expect(window.localStorage.getItem(KEY)).toBe('dark')
  })
})
