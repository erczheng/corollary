import { isApiError, isUnreachable } from './api'
import type { EngineStateResponse } from './types'

/** Whether a new position may be opened right now, and if not, why, in words.
 *
 * The one reading of the engine's halt for every surface that opens a
 * position: Research's Execute, the chain ticket's Review and the command
 * palette's Execute rows (Phase 3 step 8, U10; spec decision C12). It reads
 * the **engine's** halt from `GET /api/engine/state`. The store's `isHalted`
 * is the Phase 1 fixture halt, and nothing in production writes it, so a
 * surface reading it offers to open a position while the engine is halted.
 * `haltSource.test.ts` fails if a page goes back to it.
 *
 * Three rules, each the conservative direction:
 *
 * - **Halt closes it in every execution mode.** Halt stops new entries (rule
 *   7). A human-clicked entry while halted is still a new entry, so Manual
 *   gates exactly as Auto does.
 * - **No answer closes it, and only `halted: false` opens it.** "Not halted"
 *   is an answer only the engine can give, so while the read is in flight the
 *   gate is closed. Reading `?? false` here would enable the control in
 *   exactly that window. An answer that does not say `halted: false` (an
 *   empty body, a renamed field) is not "running" either.
 * - **A failed read closes it**, even when an earlier read said running and
 *   is still cached: that answer is no longer current. A halt that was known
 *   stays the reason, because it is the more useful sentence.
 *
 * Dismissing a candidate opens nothing and never asks this. */
export type EntryGate =
  | { open: true }
  | { open: false; kind: EntryBlock; reason: string }

export type EntryBlock = 'halted' | 'reading' | 'unavailable'

/** The fields of an engine-state query this reads: a structural subset of
 * TanStack's result, so the rule is testable without rendering anything. */
export interface EngineStateRead {
  data: EngineStateResponse | undefined
  isPending: boolean
  isError: boolean
  error: unknown
}

/** The text color for each reason, shared so the three surfaces agree.
 *
 * - A halt is `caution`: it is the engine doing what it was told, not a
 *   fault. The chain ticket already rendered it so.
 * - A failed read is `error`: a failed connection is a system failure
 *   (CLAUDE.md keeps `error` for exactly that).
 * - A read in flight is `on-surface-variant`: nothing is wrong yet. */
export const ENTRY_BLOCK_CLASS: Record<EntryBlock, string> = {
  halted: 'text-caution',
  unavailable: 'text-error',
  reading: 'text-on-surface-variant',
}

export const HALTED_REASON =
  'Trading is halted. Halt stops new entries; resume from the Dashboard to open a position.'
export const READING_REASON = 'Reading engine state. New entries wait until the engine answers.'

export function entryGate(read: EngineStateRead): EntryGate {
  if (read.data?.halted === true) {
    return { open: false, kind: 'halted', reason: HALTED_REASON }
  }
  if (read.isError) {
    return {
      open: false,
      kind: 'unavailable',
      reason: `Engine state unavailable: ${describeFailure(read.error)} ${STAY_OFF}`,
    }
  }
  if (read.isPending || read.data === undefined) {
    return { open: false, kind: 'reading', reason: READING_REASON }
  }
  // Open only on an explicit "running". An answer that says neither (a
  // renamed field, an empty body) is not one, whatever its status code.
  if (read.data.halted !== false) {
    return {
      open: false,
      kind: 'unavailable',
      reason: `Engine state unavailable: the engine's answer did not say whether it is halted. ${STAY_OFF}`,
    }
  }
  return { open: true }
}

const STAY_OFF = 'New entries stay off until the engine answers.'

const FAILED = 'The request failed.'

/** The same three cases `RequestFailed` distinguishes. Only an `ApiError`'s
 * message is shown, because that is the server's own sentence; anything else
 * could be an internal exception's text. */
function describeFailure(error: unknown): string {
  if (isUnreachable(error)) return 'The engine did not answer.'
  if (isApiError(error)) {
    const message = error.message.trim()
    if (message === '') return FAILED
    return /[.!?]$/.test(message) ? message : `${message}.`
  }
  return FAILED
}
