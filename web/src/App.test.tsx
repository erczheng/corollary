import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent, within, act } from '@testing-library/react'
import App from './App'
import { useUIStore } from './lib/store'
import { unreadCount } from './lib/notifications'

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location, and the wordmark test navigates.
  window.history.pushState({}, '', '/')
  // The bell tests mark notifications read, which is store state that would
  // otherwise leak into every test that runs after them.
  useUIStore.setState({ ...initialState }, true)
})

describe('App shell', () => {
  it('renders the wordmark and reaches all seven pages', () => {
    render(<App />)

    expect(screen.getByText('corollary')).toBeInTheDocument()

    for (const label of [
      'Dashboard',
      'Activity',
      'News',
      'Markets',
      'Research',
      'Account',
      'Settings',
    ]) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument()
    }
  })

  /** Five worded destinations on the left; Account and Settings move to the
   * tool cluster on the right as icons. The icons keep their accessible
   * names via aria-label — an icon link that announces as nothing is not a
   * link anyone can use. */
  describe('header layout', () => {
    it('keeps only the five trading pages in the main nav', () => {
      render(<App />)
      const nav = within(screen.getByRole('navigation', { name: 'Main' }))

      expect(nav.getAllByRole('link').map((l) => l.textContent)).toEqual([
        'Dashboard',
        'Activity',
        'News',
        'Markets',
        'Research',
      ])
    })

    it('puts Account and Settings outside the nav, still named', () => {
      render(<App />)
      const nav = screen.getByRole('navigation', { name: 'Main' })

      for (const label of ['Account', 'Settings']) {
        const link = screen.getByRole('link', { name: label })
        expect(nav.contains(link)).toBe(false)
        // Icon-only, so the name has to come from aria-label rather than
        // from text content.
        expect(link.textContent).toBe('')
        expect(link).toHaveAttribute('aria-label', label)
      }
    })

    it('sits notifications between Account and Settings', () => {
      render(<App />)

      const account = screen.getByRole('link', { name: 'Account' })
      // The name now carries the unread count, so match the prefix.
      const bell = screen.getByRole('button', { name: /^Notifications/ })
      const settings = screen.getByRole('link', { name: 'Settings' })

      // Node.compareDocumentPosition: 4 === "argument follows this node".
      expect(account.compareDocumentPosition(bell)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
      expect(bell.compareDocumentPosition(settings)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    })

    /** This is the test the placeholder bell asked to have changed: there is
     * a feed behind it now, so a badge is a number the panel can account for
     * rather than a decoration that lies. The count is also in the accessible
     * name — a red dot says nothing to a screen reader. */
    it('carries an unread badge that matches the feed behind it', () => {
      render(<App />)

      const bell = screen.getByRole('button', { name: /^Notifications/ })
      const unread = unreadCount(useUIStore.getState().notifications, 'paper')

      expect(unread).toBeGreaterThan(0)
      expect(bell.textContent).toBe(String(unread))
      expect(bell).toHaveAttribute('aria-label', `Notifications — ${unread} unread`)
    })

    it('drops the badge once everything visible has been read', () => {
      render(<App />)
      act(() => useUIStore.getState().markNotificationsRead())

      const bell = screen.getByRole('button', { name: 'Notifications' })
      expect(bell.textContent).toBe('')
    })
  })

  it('takes you back to the Dashboard from the wordmark', () => {
    render(<App />)

    fireEvent.click(screen.getByRole('link', { name: 'Activity' }))
    expect(screen.getByRole('heading', { name: 'Activity', level: 1 })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('link', { name: /^corollary/ }))
    expect(screen.getByRole('heading', { name: 'Portfolio Overview', level: 1 })).toBeInTheDocument()
  })

  /** The header bar is full-bleed so its bottom rule spans the window, but
   * its contents have to ride the same container as the page beneath it or
   * the wordmark stops lining up with the page title. jsdom computes no
   * layout, so this compares the two containers directly — which is the real
   * invariant anyway: they must be changed together. */
  it('rides the same container in the header as on the page', () => {
    const { container } = render(<App />)

    const containerClasses = (el: Element | null | undefined) =>
      (el?.className ?? '')
        .split(' ')
        .filter((c) => c.startsWith('max-w-') || c === 'mx-auto' || /^(lg:)?px-/.test(c))
        .sort()

    const headerRow = container.querySelector('header > div')
    const pageRow = screen.getByRole('heading', { name: 'Portfolio Overview' }).parentElement
      ?.parentElement

    expect(containerClasses(headerRow)).toEqual(containerClasses(pageRow))
    expect(containerClasses(headerRow)).toContain('max-w-[1425px]')
  })

  it('defaults to Paper and Manual', () => {
    render(<App />)

    expect(screen.getByRole('button', { name: 'Paper' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Manual' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})
