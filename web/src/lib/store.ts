import { create } from 'zustand'

export type Theme = 'light' | 'dark'
export type AccountMode = 'paper' | 'cash'
export type ExecutionMode = 'manual' | 'auto'

interface UIState {
  theme: Theme
  /** Which Alpaca account keys are in use. Paper is the only mode that may
   * come up on a cold start (CLAUDE.md rule 5) — this store is never
   * persisted to storage, so a reload always resets here. */
  accountMode: AccountMode
  /** Whether the engine may place orders unattended. */
  executionMode: ExecutionMode
  paletteOpen: boolean
  toggleTheme: () => void
  setAccountMode: (mode: AccountMode) => void
  setExecutionMode: (mode: ExecutionMode) => void
  openPalette: () => void
  closePalette: () => void
}

export const useUIStore = create<UIState>((set) => ({
  theme: 'light',
  accountMode: 'paper',
  executionMode: 'manual',
  paletteOpen: false,
  toggleTheme: () =>
    set((s) => ({ theme: s.theme === 'light' ? 'dark' : 'light' })),
  setAccountMode: (accountMode) => set({ accountMode }),
  setExecutionMode: (executionMode) => set({ executionMode }),
  openPalette: () => set({ paletteOpen: true }),
  closePalette: () => set({ paletteOpen: false }),
}))
