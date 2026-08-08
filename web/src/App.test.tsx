import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import App from './App'

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
