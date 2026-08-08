import { create } from 'zustand'
import {
  OPEN_POSITIONS,
  RECENT_ACTIVITY,
  type AccountMode,
  type ActivityItem,
  type Position,
} from './mockData'

export type Theme = 'light' | 'dark'
export type ExecutionMode = 'manual' | 'auto'
export type { AccountMode }

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
  /** Positions and activity live here for Phase 1 only, so that Flatten has
   * something real to act on. Both are server state and move to TanStack
   * Query in Phase 2 — do not grow this into a client-side position
   * ledger. */
  openPositions: Position[]
  activity: ActivityItem[]
  toggleTheme: () => void
  setAccountMode: (mode: AccountMode) => void
  setExecutionMode: (mode: ExecutionMode) => void
  setActiveStrategyId: (id: string) => void
  openPalette: () => void
  closePalette: () => void
  /** Halt and Flatten are distinct actions and never merged (CLAUDE.md
   * rule 7). Halt stops new entries and leaves every open position alone.
   * Flatten closes them all, then halts. If these two ever have the same
   * body again, one of them is wrong. */
  halt: () => void
  resume: () => void
  flatten: () => void
}

/** A long is sold to close, a short is bought to close — and each crosses
 * the spread in the opposite direction. */
function closeExecution(position: Position, at: string): ActivityItem {
  const long = position.direction === 'long'
  return {
    id: `act-flat-${position.id}`,
    time: at,
    contract: `${position.symbol} ${position.contract}`,
    action: long ? 'STC' : 'BTC',
    price: long ? position.bid : position.ask,
    quantity: position.quantity,
    pnl: position.pnl,
    amount: null, // a close reports P&L, not a cash movement
    status: 'filled',
  }
}

export const useUIStore = create<UIState>((set) => ({
  theme: 'light',
  accountMode: 'paper',
  executionMode: 'manual',
  paletteOpen: false,
  activeStrategyId: 'strat-1',
  isHalted: false,
  openPositions: OPEN_POSITIONS,
  activity: RECENT_ACTIVITY,
  toggleTheme: () =>
    set((s) => ({ theme: s.theme === 'light' ? 'dark' : 'light' })),
  setAccountMode: (accountMode) => set({ accountMode }),
  setExecutionMode: (executionMode) => set({ executionMode }),
  setActiveStrategyId: (activeStrategyId) => set({ activeStrategyId }),
  openPalette: () => set({ paletteOpen: true }),
  closePalette: () => set({ paletteOpen: false }),
  halt: () => set({ isHalted: true }),
  resume: () => set({ isHalted: false }),
  flatten: () =>
    set((s) => {
      // Every close is an execution and shows up in the activity feed, the
      // same as it would coming back from the broker.
      const at = new Date().toISOString()
      return {
        openPositions: [],
        activity: [...s.openPositions.map((p) => closeExecution(p, at)), ...s.activity],
        isHalted: true,
      }
    }),
}))
