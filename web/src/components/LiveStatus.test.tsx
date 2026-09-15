import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { LiveStatus } from './LiveStatus'

function titleFor(props: Parameters<typeof LiveStatus>[0]): string {
  render(<LiveStatus {...props} />)
  return screen.getByRole('status').getAttribute('title') ?? ''
}

const NOW = () => new Date().toISOString()

/** The poll tooltip is the only place in the UI that explains *why* the
 * Markets page does not stream, and it spent a long while explaining it with
 * the wrong number: it claimed "the websocket is capped at 30 symbols and
 * those are spent on open positions". Alpaca meters the two streams
 * separately — 30 symbols on the **equity** stream, 200 **quotes** on the
 * option stream, per CLAUDE.md — so the 30 never governed chains at all. A
 * full book of eight grouped multi-leg positions is ~32 option symbols
 * against 200; what cannot fit is a page of chains, which is what the
 * sentence needs to say. Pinned because it is prose, no other test reads it,
 * and a wrong cap here reads as authoritative. */
describe('the poll tooltip', () => {
  it('blames the option quote budget, not a 30-symbol websocket', () => {
    const title = titleFor({ kind: 'poll', at: NOW() })

    expect(title).toMatch(/200/)
    expect(title).toMatch(/option stream/)
    // The correction itself. Either spelling of the old claim is a
    // regression, and both are one careless edit away.
    expect(title).not.toMatch(/30/)
    expect(title).not.toMatch(/capped at/)
  })

  it('still says what the poll covers and that chains are excluded', () => {
    const title = titleFor({ kind: 'poll', at: NOW() })

    expect(title).toMatch(/Polled snapshots across every quoted symbol/)
    expect(title).toMatch(/Chains are not streamed/)
  })

  /** A `title` attribute that runs long stops being read, which is how a
   * tooltip ends up carrying a false claim nobody notices. The budget is a
   * design guard, not a fact about Alpaca. */
  it('stays short enough to be read', () => {
    expect(titleFor({ kind: 'poll', at: NOW() }).length).toBeLessThanOrEqual(200)
  })
})

/** The stream tooltip describes a different mechanism and carries no cap
 * claim — it should not acquire one. */
describe('the stream tooltip', () => {
  it('describes the position stream without quoting a budget', () => {
    const title = titleFor({ kind: 'stream', at: NOW() })

    expect(title).toBe('Streaming prices for the open positions in this account.')
  })

  it('reports the absence of a price rather than a limit, when stale', () => {
    // Well past the staleness threshold, and pinned to a fixed instant so
    // the test does not depend on how long the suite takes to get here.
    const title = titleFor({ kind: 'poll', at: new Date(Date.now() - 60_000).toISOString() })

    expect(title).toMatch(/not current/)
    expect(title).not.toMatch(/200|30/)
  })
})
