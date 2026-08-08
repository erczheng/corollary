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
  /** Which strategy is active — only one at a time in v1 (PRD.md §5.2). */
  activeStrategyId: string
  /** Halt stops new entries only; existing positions keep their managed
   * exits. This is UI-side display state for Phase 1 mock data — the real
   * halt is enforced by the engine, never trusted from the client
   * (CLAUDE.md rule 4). */
  isHalted: boolean
  toggleTheme: () => void
  setAccountMode: (mode: AccountMode) => void
  setExecutionMode: (mode: ExecutionMode) => void
  setActiveStrategyId: (id: string) => void
  openPalette: () => void
  closePalette: () => void
  /** Halt and Flatten are distinct actions and never merged (CLAUDE.md
   * rule 7). Flatten implies halt; halt never implies flatten. */
  halt: () => void
  resume: () => void
  flatten: () => void
}

export const useUIStore = create<UIState>((set) => ({
  theme: 'light',
  accountMode: 'paper',
  executionMode: 'manual',
  paletteOpen: false,
  activeStrategyId: 'strat-1',
  isHalted: false,
  toggleTheme: () =>
    set((s) => ({ theme: s.theme === 'light' ? 'dark' : 'light' })),
  setAccountMode: (accountMode) => set({ accountMode }),
  setExecutionMode: (executionMode) => set({ executionMode }),
  setActiveStrategyId: (activeStrategyId) => set({ activeStrategyId }),
  openPalette: () => set({ paletteOpen: true }),
  closePalette: () => set({ paletteOpen: false }),
  halt: () => set({ isHalted: true }),
  resume: () => set({ isHalted: false }),
  flatten: () => set({ isHalted: true }),
}))
