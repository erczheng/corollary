import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { RECOMMENDATIONS, recommendationTitle } from '../lib/mockData'
import type { EngineStateResponse } from '../lib/types'
import { filterCommands, type Command } from './CommandPalette'

const RUNNING: EngineStateResponse = {
  halted: false,
  haltedReason: null,
  haltedAt: null,
  t0: '2026-09-12T19:14:37.242679Z',
}

const HALTED: EngineStateResponse = { ...RUNNING, halted: true }

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

const WRITE_FAILED = jsonResponse(503, {
  error: { code: 'engine_unavailable', message: 'The engine is not reachable.' },
})

/** Answers the engine endpoints and refuses everything else.
 *
 * The palette is mounted on every page, so `GET /api/engine/state` is its
 * own read now — which command of the Halt/Resume pair to offer is a question
 * only the engine can answer. Everything else this file renders belongs to
 * the page underneath, which these tests make no claim about; refusing those
 * reads keeps a palette assertion from depending on page data.
 */
function stubFetch(engine: EngineStateResponse = RUNNING, writeAnswer?: Response) {
  let state = engine

  // `_init` is declared but read off `mock.calls` by `writes()` instead —
  // dropping the parameter would narrow the recorded call tuple to one
  // element and throw the POST bodies away.
  const fetchMock = vi.fn((input: unknown, _init?: RequestInit) => {
    const url = String(input)

    if (url.includes('/engine/halt') || url.includes('/engine/resume')) {
      if (writeAnswer) return Promise.resolve(writeAnswer)
      state = url.includes('/engine/halt') ? { ...HALTED, haltedAt: '2026-09-13T14:00:00Z' } : RUNNING
      return Promise.resolve(jsonResponse(200, state))
    }
    if (url.includes('/engine/state')) return Promise.resolve(jsonResponse(200, state))

    return Promise.resolve(
      jsonResponse(503, {
        error: { code: 'not_stubbed', message: 'Not stubbed — this file tests the palette.' },
      }),
    )
  })

  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** Every POST the palette sent, with its parsed body. Flatten's whole test is
 * that this stays empty. */
function writes(fetchMock: ReturnType<typeof stubFetch>) {
  return fetchMock.mock.calls
    .filter(([, init]) => (init as RequestInit | undefined)?.method === 'POST')
    .map(([url, init]) => ({
      url: String(url),
      body: (init as RequestInit).body === undefined
        ? undefined
        : (JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>),
    }))
}

const initialState = useUIStore.getState()
let fetchMock: ReturnType<typeof stubFetch>

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState }, true)
  queryClient.clear()
  fetchMock = stubFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
  queryClient.clear()
})

/** Render, open, and wait for the engine read to land.
 *
 * The palette offers neither Halt nor Resume until it knows which one is
 * true — guessing would mean offering Halt to an already-halted engine and
 * overwriting the reason it recorded — so a list asserted before the read
 * answers is a list with a command missing from it. */
async function openPalette(): Promise<HTMLElement> {
  render(<App />)
  act(() => useUIStore.getState().openPalette())
  const palette = screen.getByRole('dialog', { name: 'Command palette' })
  await within(palette).findByRole('option', { name: /trading/ })
  return palette
}

/** Scoped by its label: a native `<select>` also carries role `combobox`, and
 * the Dashboard has a strategy dropdown, so a bare role query matches two. */
function input(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Run a command' })
}

/** Type a query and run the highlighted result, which is what a hurried
 * Ctrl+K actually is. */
function runCommand(query: string) {
  fireEvent.change(input(), { target: { value: query } })
  fireEvent.keyDown(input(), { key: 'Enter' })
}

const command = (label: string, group = 'Pages'): Command => ({
  id: label,
  label,
  group,
  run: () => {},
})

describe('filterCommands', () => {
  const commands = [
    command('Go to Dashboard'),
    command('Halt trading', 'Engine'),
    command('Flatten all positions', 'Engine'),
  ]

  it('returns everything for an empty query', () => {
    expect(filterCommands(commands, '')).toHaveLength(3)
    expect(filterCommands(commands, '   ')).toHaveLength(3)
  })

  it('matches on label, case-insensitively', () => {
    expect(filterCommands(commands, 'HALT').map((c) => c.label)).toEqual(['Halt trading'])
  })

  it('matches on group, so typing a category narrows to it', () => {
    expect(filterCommands(commands, 'engine')).toHaveLength(2)
  })

  it('returns nothing when nothing matches', () => {
    expect(filterCommands(commands, 'zzz')).toEqual([])
  })

  /** Not fuzzy, deliberately. On a list this small fuzzy matching mostly means
   * a typo silently highlights a *different* command — and one of them closes
   * every position. */
  it('does not match a subsequence the way a fuzzy matcher would', () => {
    expect(filterCommands(commands, 'hlt')).toEqual([])
  })
})

describe('opening the palette', () => {
  it('opens on Ctrl+K from anywhere', () => {
    render(<App />)
    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(screen.getByRole('dialog', { name: 'Command palette' })).toBeInTheDocument()
  })

  it('closes on Escape', async () => {
    await openPalette()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()
  })

  /** Reopening onto the last query and the last highlighted row is how Enter
   * runs something you never read. */
  it('opens fresh rather than restoring the last query', async () => {
    await openPalette()
    fireEvent.change(input(), { target: { value: 'flatten' } })

    act(() => useUIStore.getState().closePalette())
    act(() => useUIStore.getState().openPalette())

    expect(input()).toHaveValue('')
  })
})

describe('the command list', () => {
  it('offers every page', async () => {
    const palette = await openPalette()
    for (const page of ['Dashboard', 'Activity', 'News', 'Markets', 'Research', 'Account', 'Settings']) {
      expect(within(palette).getByRole('option', { name: new RegExp(`Go to ${page}`) })).toBeInTheDocument()
    }
  })

  it('offers the engine controls, keeping Halt and Flatten separate', async () => {
    const palette = await openPalette()
    expect(within(palette).getByRole('option', { name: /Halt trading/ })).toBeInTheDocument()
    expect(within(palette).getByRole('option', { name: /Flatten all positions/ })).toBeInTheDocument()
  })

  it('offers Resume instead of Halt once the engine reports halted', async () => {
    fetchMock = stubFetch(HALTED)
    const palette = await openPalette()

    expect(within(palette).getByRole('option', { name: /Resume trading/ })).toBeInTheDocument()
    expect(within(palette).queryByRole('option', { name: /Halt trading/ })).not.toBeInTheDocument()
  })

  /** Server state, not the store's. The Dashboard's pill reads the API, and a
   * palette that read the Phase 1 flag would offer Halt to an engine already
   * halted by the dead-man's switch — overwriting the reason it recorded. */
  it('reads the halt from the engine, not from the fixture store', async () => {
    fetchMock = stubFetch(HALTED)
    render(<App />)
    act(() => {
      // The store says running. The engine says halted. The engine wins.
      useUIStore.getState().resume()
      useUIStore.getState().openPalette()
    })

    const palette = screen.getByRole('dialog', { name: 'Command palette' })
    expect(await within(palette).findByRole('option', { name: /Resume trading/ })).toBeInTheDocument()
  })

  it('offers to execute a named recommendation', async () => {
    const palette = await openPalette()
    const title = recommendationTitle(RECOMMENDATIONS[0])
    // Matched on text rather than a regex: the title contains `$` and `/`,
    // which are regex metacharacters, and escaping them here would be testing
    // the escape rather than the palette.
    const option = within(palette)
      .getAllByRole('option')
      .find((o) => o.textContent?.startsWith(`Execute ${title}`))

    expect(option).toBeDefined()
    expect(option!.textContent).toContain('Submits to the risk manager')
  })

  /** Acting on a candidate you cannot see is acting on a stale list. */
  it('drops a dismissed recommendation from the list', async () => {
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().dismissRecommendation(target.id))
    const palette = await openPalette()

    expect(
      within(palette).queryByRole('option', { name: new RegExp(`Execute ${target.symbol}`) }),
    ).not.toBeInTheDocument()
  })

  it('stops offering Execute once a recommendation has been executed', async () => {
    const target = RECOMMENDATIONS[0]
    act(() => useUIStore.getState().executeRecommendation(target.id))
    const palette = await openPalette()

    expect(
      within(palette).queryByRole('option', { name: new RegExp(`Execute ${target.symbol}`) }),
    ).not.toBeInTheDocument()
    // Still dismissable — it is still on the board.
    expect(
      within(palette).getByRole('option', { name: new RegExp(`Dismiss ${target.symbol}`) }),
    ).toBeInTheDocument()
  })
})

describe('filtering and keyboard navigation', () => {
  it('narrows the list as you type', async () => {
    const palette = await openPalette()
    fireEvent.change(input(), { target: { value: 'go to set' } })

    const options = within(palette).getAllByRole('option')
    expect(options).toHaveLength(1)
    expect(options[0].textContent).toContain('Go to Settings')
  })

  it('says so when nothing matches', async () => {
    await openPalette()
    fireEvent.change(input(), { target: { value: 'zzzz' } })
    expect(screen.getByText(/No command matches/)).toBeInTheDocument()
  })

  it('selects the first result by default and moves with the arrows', async () => {
    const palette = await openPalette()
    const options = () => within(palette).getAllByRole('option')

    expect(options()[0]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(input(), { key: 'ArrowDown' })
    expect(options()[1]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(input(), { key: 'ArrowUp' })
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('does not move past either end of the list', async () => {
    const palette = await openPalette()
    const options = () => within(palette).getAllByRole('option')

    fireEvent.keyDown(input(), { key: 'ArrowUp' })
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')

    for (let i = 0; i < options().length + 5; i += 1) {
      fireEvent.keyDown(input(), { key: 'ArrowDown' })
    }
    const all = options()
    expect(all[all.length - 1]).toHaveAttribute('aria-selected', 'true')
  })

  it('runs the highlighted command on Enter and closes', async () => {
    await openPalette()
    runCommand('go to settings')

    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
  })

  it('runs a command on click', async () => {
    const palette = await openPalette()
    fireEvent.click(within(palette).getByRole('option', { name: /Go to Markets/ }))

    expect(screen.getByRole('heading', { name: 'Markets', level: 1 })).toBeInTheDocument()
  })
})

describe('engine commands', () => {
  /** Halt is a real write in this phase, and rule 8 wants the reason with it:
   * "Halted by hand" and "the dead-man's switch fired" are different answers
   * to the same question, and only the recorded reason tells them apart. */
  it('halts through the engine, with a recorded reason', async () => {
    await openPalette()
    runCommand('halt')

    await screen.findByRole('alertdialog')
    expect(writes(fetchMock)).toEqual([
      { url: '/api/engine/halt', body: { reason: 'Halted by hand from the command palette' } },
    ])
  })

  /** The palette closes before the request settles, so without this a halt
   * taken from any page but the Dashboard would report nothing at all — and
   * a halt you believe you took is the state rule 9 exists to keep visible. */
  it('states the outcome of a halt, and that nothing was closed', async () => {
    await openPalette()
    runCommand('halt')

    const notice = await screen.findByRole('alertdialog')
    expect(notice.textContent).toMatch(/Engine halted/)
    expect(notice.textContent).toMatch(/nothing was closed/)
  })

  it('resumes through the engine — an explicit human action, per rule 9', async () => {
    fetchMock = stubFetch(HALTED)
    await openPalette()
    runCommand('resume')

    const notice = await screen.findByRole('alertdialog')
    expect(notice.textContent).toMatch(/Engine resumed/)
    expect(writes(fetchMock).map((w) => w.url)).toEqual(['/api/engine/resume'])
  })

  /** A write that failed has to say so. It is `error` — a fault, not a
   * limitation — and it names the state the engine is *not* in, because
   * believing you halted is the whole hazard. */
  it('reports a failed halt rather than reporting nothing', async () => {
    fetchMock = stubFetch(RUNNING, WRITE_FAILED)
    await openPalette()
    runCommand('halt')

    const notice = await screen.findByRole('alertdialog')
    expect(notice.textContent).toMatch(/Halt request failed/)
    expect(notice.textContent).toMatch(/The engine is not reachable\./)
    expect(notice.textContent).toMatch(/still running/)
    expect(within(notice).getByRole('heading')).toHaveClass('text-error')
  })

  it('executes a recommendation from the palette', async () => {
    const target = RECOMMENDATIONS[0]
    await openPalette()
    runCommand(`execute ${target.symbol}`)

    expect(useUIStore.getState().dispositions[target.id]).toBe('executed')
  })
})

/** The one that matters.
 *
 * Flatten used to call `store.flatten()` — which empties a fixture book that
 * no page renders any more. The confirm said "this cannot be undone", you
 * accepted it, and not one real position moved. A destructive command that
 * reports success and does nothing is worse than one that errors, because the
 * dialog tells you it worked.
 */
describe('flatten, which this phase cannot do', () => {
  it('stays in the list, so the control is still findable', async () => {
    const palette = await openPalette()
    const option = within(palette).getByRole('option', { name: /Flatten all positions/ })

    // The row says it before you press Enter. Omitting the command instead
    // would answer "No command matches “flatten”", which is the one reply
    // that leaves you looking for another route to it.
    expect(option.textContent).toMatch(/Unavailable until Phase 6/)
  })

  it('sends nothing and states that nothing was closed', async () => {
    await openPalette()
    runCommand('flatten')

    const notice = await screen.findByRole('alertdialog')
    expect(notice.textContent).toMatch(/nothing was closed/i)
    expect(notice.textContent).toMatch(/Phase 6/)
    expect(notice.textContent).toMatch(/still open/)
    // No order, no halt, no write of any kind.
    expect(writes(fetchMock)).toEqual([])
  })

  /** A capability this phase does not have is `caution`, never `error`:
   * nothing is broken. `bearish` is a loss and `error` is a fault. */
  it('reads as a limitation, not a fault', async () => {
    await openPalette()
    runCommand('flatten')

    const notice = await screen.findByRole('alertdialog')
    expect(within(notice).getByRole('heading')).toHaveClass('text-caution')
  })

  /** The structural pin, kept from the confirm this notice replaces.
   *
   * `if (!open) return null` used to sit above the palette's return, so
   * running Flatten — whose first act is to close the palette — unmounted the
   * dialog in the same tick it was asked for, and the most destructive
   * command in the list had no guard behind it at all. The dialog's contents
   * changed; the hazard did not. It must still outlive the palette. */
  it('shows its dialog even though the palette closes in the same tick', async () => {
    await openPalette()
    runCommand('flatten')

    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()
    expect(await screen.findByRole('alertdialog')).toBeInTheDocument()
  })

  it('dismisses on Escape and on the button', async () => {
    await openPalette()
    runCommand('flatten')

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    act(() => useUIStore.getState().openPalette())
    runCommand('flatten')
    const notice = await screen.findByRole('alertdialog')
    fireEvent.click(within(notice).getByRole('button', { name: 'Dismiss' }))

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

/** Two actions, not three. `Queue` was removed, so accepting a candidate
 * means submitting it and nothing else. */
describe('recommendation actions', () => {
  const first = RECOMMENDATIONS[0]
  const title = recommendationTitle(first)

  it('offers Execute and Dismiss, and no Queue', async () => {
    const dialog = await openPalette()

    expect(within(dialog).getByText(`Execute ${title}`)).toBeInTheDocument()
    expect(within(dialog).getByText(`Dismiss ${title}`)).toBeInTheDocument()
    expect(within(dialog).queryByText(`Queue ${title}`)).not.toBeInTheDocument()
  })

  it('Execute records the submission', async () => {
    const dialog = await openPalette()
    fireEvent.click(within(dialog).getByText(`Execute ${title}`))

    expect(useUIStore.getState().dispositions[first.id]).toBe('executed')
  })

  /** Halting stops new entries (CLAUDE.md rule 7). A surface that still
   * offers to open one is the surface it happens on by accident. */
  describe('while trading is halted', () => {
    it('withdraws Execute on the fixture halt the Research page reads', async () => {
      act(() => useUIStore.getState().halt())
      const dialog = await openPalette()

      expect(within(dialog).queryByText(`Execute ${title}`)).not.toBeInTheDocument()
    })

    /** Either halt withdraws it — the gate is only ever more conservative,
     * so halting from this very palette stops the Execute rows under it. */
    it('withdraws Execute on the engine halt as well', async () => {
      fetchMock = stubFetch(HALTED)
      const dialog = await openPalette()

      expect(within(dialog).queryByText(`Execute ${title}`)).not.toBeInTheDocument()
    })

    /** Waving off a candidate opens nothing, so it is never gated. */
    it('keeps Dismiss available', async () => {
      act(() => useUIStore.getState().halt())
      const dialog = await openPalette()

      expect(within(dialog).getByText(`Dismiss ${title}`)).toBeInTheDocument()
    })

    it('brings Execute back on resume', async () => {
      act(() => useUIStore.getState().halt())
      act(() => useUIStore.getState().resume())
      const dialog = await openPalette()

      expect(within(dialog).getByText(`Execute ${title}`)).toBeInTheDocument()
    })
  })
})
