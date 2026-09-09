import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, within, fireEvent } from '@testing-library/react'
import App from '../App'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS } from '../lib/mockData'
import { formatUsd } from '../lib/format'

const initialState = useUIStore.getState()
const CASH = ACCOUNT_SNAPSHOTS.cash

beforeEach(() => {
  window.history.pushState({}, '', '/')
  useUIStore.setState({ ...initialState }, true)
})

/** The switch is on the Dashboard, Activity and Account — every page whose
 * whole contents are account-scoped (PRD.md §2). Scoped to `main` because the
 * header also carries the word Cash, as a read-only badge. */
function cashButton(): HTMLElement {
  return within(screen.getByRole('main')).getByRole('button', { name: 'Cash' })
}

function dialog(): HTMLElement {
  return screen.getByRole('alertdialog')
}

describe('AccountModeToggle', () => {
  it('comes up in Paper on a cold start', () => {
    render(<App />)

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(within(screen.getByRole('main')).getByRole('button', { name: 'Paper' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('does not switch on the click alone — it asks first', () => {
    render(<App />)
    fireEvent.click(cashButton())

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(dialog()).toBeInTheDocument()
  })

  /** PRD.md §2 and CLAUDE.md rule 5: the confirm names the account **and its
   * balance**. It used to say "balance not yet available", which was written
   * before the Account page existed and stopped being true when it did. */
  describe('the confirm', () => {
    beforeEach(() => {
      render(<App />)
      fireEvent.click(cashButton())
    })

    it('names the account', () => {
      expect(within(dialog()).getByText(/live Alpaca keys/)).toBeInTheDocument()
      expect(within(dialog()).getByText(/real money/)).toBeInTheDocument()
    })

    /** Buying power leads. On a cash account it is the *settled* balance, so
     * quoting cash alone in the dialog that precedes real trading would
     * overstate what can actually be deployed — here by $240. */
    it('quotes buying power, not cash alone', () => {
      const text = dialog().textContent ?? ''

      expect(text).toContain(formatUsd(CASH.buyingPower))
      expect(text).toContain(formatUsd(CASH.cash))
      expect(text).toMatch(/buying power/i)
    })

    it('accounts for the gap between them rather than leaving it unexplained', () => {
      expect(CASH.cash - CASH.buyingPower).toBeCloseTo(CASH.unsettled, 2)
      expect(dialog().textContent ?? '').toContain(formatUsd(CASH.unsettled))
      expect(within(dialog()).getByText(/unsettled/)).toBeInTheDocument()
    })

    /** The figures are the ones the Account page shows for the same book, so
     * the dialog cannot quote a balance that page disagrees with. */
    it('quotes the same numbers the Account page does', () => {
      const text = dialog().textContent ?? ''

      expect(text).not.toContain(formatUsd(ACCOUNT_SNAPSHOTS.paper.buyingPower))
      expect(text).toContain(formatUsd(ACCOUNT_SNAPSHOTS.cash.buyingPower))
    })

    it('leaves the account alone when cancelled', () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: 'Cancel' }))

      expect(useUIStore.getState().accountMode).toBe('paper')
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    })

    it('switches only on the explicit confirm', () => {
      fireEvent.click(within(dialog()).getByRole('button', { name: 'Switch to Cash' }))

      expect(useUIStore.getState().accountMode).toBe('cash')
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
    })
  })

  /** Going back is not the dangerous direction, so it does not ask. */
  it('returns to Paper without a confirm', () => {
    useUIStore.setState({ accountMode: 'cash' })
    render(<App />)

    fireEvent.click(within(screen.getByRole('main')).getByRole('button', { name: 'Paper' }))

    expect(useUIStore.getState().accountMode).toBe('paper')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})
