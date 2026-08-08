import { create } from 'zustand'
import {
  ACCOUNT_SNAPSHOTS,
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
  /** Positions and activity live here for Phase 1 only, so that Flatten and
   * Close have something real to act on. Both are server state and move to
   * TanStack Query in Phase 2 — do not grow this into a client-side
   * position ledger.
   *
   * Keyed by account: Paper and Cash are different accounts holding
   * different money, and only one account's keys are in use at a time. Read
   * these as `openPositions[accountMode]`, never as a merged book. */
  openPositions: Record<AccountMode, Position[]>
  activity: Record<AccountMode, ActivityItem[]>
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
  /** Close one position — the per-row action on Activity (PRD.md §8.2).
   * Distinct from Flatten in the same way Flatten is distinct from Halt:
   * closing a single position is not a book-wide event and must never
   * halt the engine. */
  closePosition: (id: string) => void
}

/** A long is sold to close, a short is bought to close — and each crosses
 * the spread in the opposite direction.
 *
 * `idPrefix` records *why* the position closed: `act-flat-` for a book-wide
 * Flatten, `act-close-` for a single row closed from Activity. Same fill,
 * different decision, and the audit trail should be able to tell them
 * apart. */
function closeExecution(position: Position, at: string, idPrefix: string): ActivityItem {
  const long = position.direction === 'long'
  return {
    id: `${idPrefix}${position.id}`,
    time: at,
    contract: `${position.symbol} ${position.contract}`,
    action: long ? 'STC' : 'BTC',
    price: long ? position.bid : position.ask,
    quantity: position.quantity,
    pnl: position.pnl,
    pnlPct: position.pnlPct,
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
  openPositions: {
    paper: ACCOUNT_SNAPSHOTS.paper.positions,
    cash: ACCOUNT_SNAPSHOTS.cash.positions,
  },
  activity: {
    paper: ACCOUNT_SNAPSHOTS.paper.activity,
    cash: ACCOUNT_SNAPSHOTS.cash.activity,
  },
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
      // Only the active account's book is touched. That is not a partial
      // flatten: `accountMode` is which Alpaca keys are in use, so the
      // engine only ever holds the positions in that account. The other
      // book belongs to credentials that aren't loaded.
      const mode = s.accountMode
      // Every close is an execution and shows up in the activity feed, the
      // same as it would coming back from the broker.
      const at = new Date().toISOString()
      return {
        openPositions: { ...s.openPositions, [mode]: [] },
        activity: {
          ...s.activity,
          [mode]: [...s.openPositions[mode].map((p) => closeExecution(p, at, 'act-flat-')), ...s.activity[mode]],
        },
        isHalted: true,
      }
    }),
  closePosition: (id) =>
    set((s) => {
      const mode = s.accountMode
      const position = s.openPositions[mode].find((p) => p.id === id)
      if (!position) return s

      // Note what this does *not* do: it never sets isHalted. Closing one
      // position is not a book-wide event (CLAUDE.md rule 7).
      const at = new Date().toISOString()
      return {
        openPositions: { ...s.openPositions, [mode]: s.openPositions[mode].filter((p) => p.id !== id) },
        activity: { ...s.activity, [mode]: [closeExecution(position, at, 'act-close-'), ...s.activity[mode]] },
      }
    }),
}))
