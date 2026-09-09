import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, act } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { RECOMMENDATIONS, recommendationTitle } from '../lib/mockData'
import { filterCommands, type Command } from './CommandPalette'

const initialState = useUIStore.getState()

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState }, true)
})

function openPalette(): HTMLElement {
  render(<App />)
  act(() => useUIStore.getState().openPalette())
  return screen.getByRole('dialog', { name: 'Command palette' })
}

/** Scoped by its label: a native `<select>` also carries role `combobox`, and
 * the Dashboard has a strategy dropdown, so a bare role query matches two. */
function input(): HTMLElement {
  return screen.getByRole('combobox', { name: 'Run a command' })
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

  it('closes on Escape', () => {
    openPalette()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()
  })

  /** Reopening onto the last query and the last highlighted row is how Enter
   * runs something you never read. */
  it('opens fresh rather than restoring the last query', () => {
    openPalette()
    fireEvent.change(input(), { target: { value: 'flatten' } })

    act(() => useUIStore.getState().closePalette())
    act(() => useUIStore.getState().openPalette())

    expect(input()).toHaveValue('')
  })
})

describe('the command list', () => {
  it('offers every page', () => {
    const palette = openPalette()
    for (const page of ['Dashboard', 'Activity', 'News', 'Markets', 'Research', 'Account', 'Settings']) {
      expect(within(palette).getByRole('option', { name: new RegExp(`Go to ${page}`) })).toBeInTheDocument()
    }
  })

  it('offers the engine controls, keeping Halt and Flatten separate', () => {
    const palette = openPalette()
    expect(within(palette).getByRole('option', { name: /Halt trading/ })).toBeInTheDocument()
    expect(within(palette).getByRole('option', { name: /Flatten all positions/ })).toBeInTheDocument()
  })

  it('offers Resume instead of Halt once halted', () => {
    render(<App />)
    act(() => {
      useUIStore.getState().halt()
      useUIStore.getState().openPalette()
    })

    const palette = screen.getByRole('dialog', { name: 'Command palette' })
    expect(within(palette).getByRole('option', { name: /Resume trading/ })).toBeInTheDocument()
    expect(within(palette).queryByRole('option', { name: /Halt trading/ })).not.toBeInTheDocument()
  })

  it('offers to execute a named recommendation', () => {
    const palette = openPalette()
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
  it('drops a dismissed recommendation from the list', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => {
      useUIStore.getState().dismissRecommendation(target.id)
      useUIStore.getState().openPalette()
    })

    const palette = screen.getByRole('dialog', { name: 'Command palette' })
    expect(
      within(palette).queryByRole('option', { name: new RegExp(`Execute ${target.symbol}`) }),
    ).not.toBeInTheDocument()
  })

  it('stops offering Execute once a recommendation has been executed', () => {
    render(<App />)
    const target = RECOMMENDATIONS[0]
    act(() => {
      useUIStore.getState().executeRecommendation(target.id)
      useUIStore.getState().openPalette()
    })

    const palette = screen.getByRole('dialog', { name: 'Command palette' })
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
  it('narrows the list as you type', () => {
    const palette = openPalette()
    fireEvent.change(input(), { target: { value: 'go to set' } })

    const options = within(palette).getAllByRole('option')
    expect(options).toHaveLength(1)
    expect(options[0].textContent).toContain('Go to Settings')
  })

  it('says so when nothing matches', () => {
    openPalette()
    fireEvent.change(input(), { target: { value: 'zzzz' } })
    expect(screen.getByText(/No command matches/)).toBeInTheDocument()
  })

  it('selects the first result by default and moves with the arrows', () => {
    const palette = openPalette()
    const options = () => within(palette).getAllByRole('option')

    expect(options()[0]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(input(), { key: 'ArrowDown' })
    expect(options()[1]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(input(), { key: 'ArrowUp' })
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')
  })

  it('does not move past either end of the list', () => {
    const palette = openPalette()
    const options = () => within(palette).getAllByRole('option')

    fireEvent.keyDown(input(), { key: 'ArrowUp' })
    expect(options()[0]).toHaveAttribute('aria-selected', 'true')

    for (let i = 0; i < options().length + 5; i += 1) {
      fireEvent.keyDown(input(), { key: 'ArrowDown' })
    }
    const all = options()
    expect(all[all.length - 1]).toHaveAttribute('aria-selected', 'true')
  })

  it('runs the highlighted command on Enter and closes', () => {
    openPalette()
    fireEvent.change(input(), { target: { value: 'go to settings' } })
    fireEvent.keyDown(input(), { key: 'Enter' })

    expect(screen.queryByRole('dialog', { name: 'Command palette' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Settings', level: 1 })).toBeInTheDocument()
  })

  it('runs a command on click', () => {
    const palette = openPalette()
    fireEvent.click(within(palette).getByRole('option', { name: /Go to Markets/ }))

    expect(screen.getByRole('heading', { name: 'Markets', level: 1 })).toBeInTheDocument()
  })
})

describe('engine commands', () => {
  it('halts from the palette', () => {
    openPalette()
    fireEvent.change(input(), { target: { value: 'halt' } })
    fireEvent.keyDown(input(), { key: 'Enter' })

    expect(useUIStore.getState().isHalted).toBe(true)
  })

  it('resumes from the palette', () => {
    render(<App />)
    act(() => {
      useUIStore.getState().halt()
      useUIStore.getState().openPalette()
    })

    fireEvent.change(input(), { target: { value: 'resume' } })
    fireEvent.keyDown(input(), { key: 'Enter' })

    expect(useUIStore.getState().isHalted).toBe(false)
  })

  /** The one that matters. Flatten is reachable by typing three letters and
   * pressing Enter, which makes it the easiest destructive action in the app to
   * fire by accident. */
  it('asks before flattening, and does not flatten until confirmed', () => {
    const before = useUIStore.getState().openPositions.paper.length
    expect(before).toBeGreaterThan(0)

    openPalette()
    fireEvent.change(input(), { target: { value: 'flatten' } })
    fireEvent.keyDown(input(), { key: 'Enter' })

    const dialog = screen.getByRole('alertdialog')
    expect(dialog.textContent).toMatch(/cannot be undone/)
    expect(useUIStore.getState().openPositions.paper).toHaveLength(before)

    fireEvent.click(within(dialog).getByRole('button', { name: 'Flatten and halt' }))

    expect(useUIStore.getState().openPositions.paper).toHaveLength(0)
    expect(useUIStore.getState().isHalted).toBe(true)
  })

  it('leaves the book alone when the flatten confirm is cancelled', () => {
    const before = useUIStore.getState().openPositions.paper.length

    openPalette()
    fireEvent.change(input(), { target: { value: 'flatten' } })
    fireEvent.keyDown(input(), { key: 'Enter' })
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    expect(useUIStore.getState().openPositions.paper).toHaveLength(before)
    expect(useUIStore.getState().isHalted).toBe(false)
  })

  it('executes a recommendation from the palette', () => {
    const target = RECOMMENDATIONS[0]
    openPalette()
    fireEvent.change(input(), { target: { value: `execute ${target.symbol}` } })
    fireEvent.keyDown(input(), { key: 'Enter' })

    expect(useUIStore.getState().dispositions[target.id]).toBe('executed')
  })
})


/** Two actions, not three. `Queue` was removed, so accepting a candidate
 * means submitting it and nothing else. */
describe('recommendation actions', () => {
  const first = RECOMMENDATIONS[0]
  const title = recommendationTitle(first)

  it('offers Execute and Dismiss, and no Queue', () => {
    const dialog = openPalette()

    expect(within(dialog).getByText(`Execute ${title}`)).toBeInTheDocument()
    expect(within(dialog).getByText(`Dismiss ${title}`)).toBeInTheDocument()
    expect(within(dialog).queryByText(`Queue ${title}`)).not.toBeInTheDocument()
  })

  it('Execute records the submission', () => {
    const dialog = openPalette()
    fireEvent.click(within(dialog).getByText(`Execute ${title}`))

    expect(useUIStore.getState().dispositions[first.id]).toBe('executed')
  })

  /** Halting stops new entries (CLAUDE.md rule 7). A surface that still
   * offers to open one is the surface it happens on by accident. */
  describe('while the engine is halted', () => {
    it('withdraws Execute', () => {
      act(() => useUIStore.getState().halt())
      const dialog = openPalette()

      expect(within(dialog).queryByText(`Execute ${title}`)).not.toBeInTheDocument()
    })

    /** Waving off a candidate opens nothing, so it is never gated. */
    it('keeps Dismiss available', () => {
      act(() => useUIStore.getState().halt())
      const dialog = openPalette()

      expect(within(dialog).getByText(`Dismiss ${title}`)).toBeInTheDocument()
    })

    it('brings Execute back on resume', () => {
      act(() => useUIStore.getState().halt())
      act(() => useUIStore.getState().resume())
      const dialog = openPalette()

      expect(within(dialog).getByText(`Execute ${title}`)).toBeInTheDocument()
    })
  })
})
