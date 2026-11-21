import { describe, expect, it } from 'vitest'
import { ApiError, NETWORK_UNREACHABLE } from './api'
import { entryGate, type EngineStateRead } from './entryGate'
import type { EngineStateResponse } from './types'

const RUNNING: EngineStateResponse = {
  halted: false,
  haltedReason: null,
  haltedAt: null,
  t0: '2026-09-12T19:14:37.242679Z',
}
const HALTED: EngineStateResponse = {
  ...RUNNING,
  halted: true,
  haltedReason: 'Halted by hand from the command palette',
  haltedAt: '2026-11-17T14:00:00Z',
}

const read = (over: Partial<EngineStateRead>): EngineStateRead => ({
  data: undefined,
  isPending: false,
  isError: false,
  error: null,
  ...over,
})

const apiError = (code: string, message: string) =>
  new ApiError({ status: 503, code, message, url: '/api/engine/state' })

/** Spec decision C12: one reading of the halt for every surface that opens a
 * position, and it fails closed. Halt stops new entries (rule 7), whoever
 * clicks, and "not halted" is an answer only the engine can give. */
describe('entryGate', () => {
  it('is open only when the engine has answered that it is running', () => {
    expect(entryGate(read({ data: RUNNING }))).toEqual({ open: true })
  })

  it('is closed while the engine is halted, and says so', () => {
    const gate = entryGate(read({ data: HALTED }))
    expect(gate.open).toBe(false)
    if (gate.open) return
    expect(gate.kind).toBe('halted')
    expect(gate.reason).toMatch(/Trading is halted/)
    expect(gate.reason).toMatch(/resume/i)
  })

  /** The `?? false` this replaces read "not halted" before anyone had
   * answered, and enabled the control in that window. */
  it('is closed while the engine state is still being read', () => {
    const gate = entryGate(read({ isPending: true }))
    expect(gate.open).toBe(false)
    if (gate.open) return
    expect(gate.kind).toBe('reading')
    expect(gate.reason).toMatch(/^Reading engine state/)
  })

  it('is closed when there is no answer, even if the query claims to be settled', () => {
    expect(entryGate(read({})).open).toBe(false)
  })

  /** "Open" needs the engine to have said `halted: false`. An answer that
   * says neither, such as a renamed field or an empty body, is not "running". */
  it.each([
    ['an empty body', {}],
    ['a renamed field', { isHalted: false }],
    ['a non-boolean', { halted: 'false' }],
    ['a null', { halted: null }],
  ])('is closed on %s, and says the answer was unreadable', (_name, body) => {
    const gate = entryGate(read({ data: body as unknown as EngineStateResponse }))
    expect(gate.open).toBe(false)
    if (gate.open) return
    expect(gate.kind).toBe('unavailable')
    expect(gate.reason).toMatch(/^Engine state unavailable: /)
    expect(gate.reason).toMatch(/did not say whether it is halted/)
  })

  it("is closed when the read failed, and carries the server's own sentence", () => {
    const gate = entryGate(
      read({ isError: true, error: apiError('engine_unavailable', 'The engine is not reachable.') }),
    )
    expect(gate.open).toBe(false)
    if (gate.open) return
    expect(gate.kind).toBe('unavailable')
    expect(gate.reason).toMatch(/^Engine state unavailable: The engine is not reachable\./)
  })

  it('names an unreachable engine in plain words', () => {
    const gate = entryGate(
      read({ isError: true, error: apiError(NETWORK_UNREACHABLE, 'Could not reach it at /x.') }),
    )
    if (gate.open) throw new Error('expected a closed gate')
    expect(gate.reason).toMatch(/^Engine state unavailable: The engine did not answer\./)
  })

  it('does not echo an error it cannot vouch for', () => {
    const gate = entryGate(read({ isError: true, error: new Error('TypeError: x is undefined') }))
    if (gate.open) throw new Error('expected a closed gate')
    expect(gate.reason).not.toMatch(/TypeError/)
    expect(gate.reason).toMatch(/^Engine state unavailable: /)
  })

  /** A refetch that fails after a good read leaves the last answer in
   * `data`. That answer is no longer current, so it opens nothing. */
  it('is closed when a later read failed, even though an earlier one said running', () => {
    const gate = entryGate(
      read({ data: RUNNING, isError: true, error: apiError('engine_unavailable', 'Down.') }),
    )
    expect(gate.open).toBe(false)
    if (gate.open) return
    expect(gate.kind).toBe('unavailable')
  })

  /** Both close the gate. The known halt is the more useful sentence. */
  it('reports the halt when the last answer was halted and a later read failed', () => {
    const gate = entryGate(
      read({ data: HALTED, isError: true, error: apiError('engine_unavailable', 'Down.') }),
    )
    if (gate.open) throw new Error('expected a closed gate')
    expect(gate.kind).toBe('halted')
  })

  it('adds the full stop a server sentence left off', () => {
    const gate = entryGate(read({ isError: true, error: apiError('x', 'Down') }))
    if (gate.open) throw new Error('expected a closed gate')
    expect(gate.reason).toMatch(/^Engine state unavailable: Down\. /)
  })

  it('never prints an empty sentence for an error with no message', () => {
    const gate = entryGate(read({ isError: true, error: apiError('x', '   ') }))
    if (gate.open) throw new Error('expected a closed gate')
    expect(gate.reason).toMatch(/^Engine state unavailable: The request failed\. /)
  })
})
