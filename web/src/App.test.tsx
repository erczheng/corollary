import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import App from './App'

beforeEach(() => {
  // BrowserRouter reads window.location, and the wordmark test navigates.
  window.history.pushState({}, '', '/')
})

describe('App shell', () => {
  it('renders the wordmark and all seven page links', () => {
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
