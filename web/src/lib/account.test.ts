import { describe, it, expect } from 'vitest'
import { ACCOUNT_SNAPSHOTS, CONTRACT_MULTIPLIER, isOrderAction, type AccountMode } from './mockData'
import { cashTransfers, netTransfers, positionsValue, totalEquity } from './account'

const MODES: AccountMode[] = ['paper', 'cash']

/** The three balances the Account page reconciles against. Each relationship
 * below is a claim about what can actually be deployed, and getting one
 * backwards overstates capacity — which is the only direction that costs
 * money. Pinned per account because they are separate books.
 *
 * A `settled`/`unsettled` split used to be pinned here too. It was removed
 * with the fields: Alpaca publishes no settlement breakdown, so the invariant
 * was asserting arithmetic over two numbers nothing could verify. See
 * PRD.md §8.6. */
describe('balance fixtures', () => {
  /** Options are not marginable. On a margin account buying power is 2× cash
   * while options buying power is not, and sizing an option order against the
   * wrong one overstates capacity by 2×. */
  it.each(MODES)('never reports more options buying power than buying power (%s)', (mode) => {
    const a = ACCOUNT_SNAPSHOTS[mode]
    expect(a.optionsBuyingPower).toBeLessThanOrEqual(a.buyingPower)
  })

  /** No margin means buying power cannot exceed the balance, and here it is
   * strictly below it — the broker is holding something back. The fixture
   * keeps that gap because a real cash account has one; what it no longer
   * does is name a cause Alpaca doesn't report. */
  it('gives the cash account no margin — buying power is below cash', () => {
    const a = ACCOUNT_SNAPSHOTS.cash
    expect(a.buyingPower).toBeLessThan(a.cash)
  })

  it('gives the paper margin account more buying power than cash', () => {
    const a = ACCOUNT_SNAPSHOTS.paper
    expect(a.buyingPower).toBeGreaterThan(a.cash)
  })
})

describe('cashTransfers', () => {
  it.each(MODES)('keeps cash movements and drops orders (%s)', (mode) => {
    const transfers = cashTransfers(ACCOUNT_SNAPSHOTS[mode].activity)

    expect(transfers.length).toBeGreaterThan(0)
    for (const t of transfers) {
      expect(isOrderAction(t.action)).toBe(false)
      expect(t.amount).not.toBeNull()
    }
  })

  it('reads the account it is given rather than a merged book', () => {
    const paper = cashTransfers(ACCOUNT_SNAPSHOTS.paper.activity).map((t) => t.id)
    const cash = cashTransfers(ACCOUNT_SNAPSHOTS.cash.activity).map((t) => t.id)

    expect(paper).not.toEqual(cash)
    expect(paper.some((id) => cash.includes(id))).toBe(false)
  })

  it('finds both a deposit and a withdrawal in the paper book', () => {
    const actions = cashTransfers(ACCOUNT_SNAPSHOTS.paper.activity).map((t) => t.action)
    expect(actions).toContain('DEPOSIT')
    expect(actions).toContain('WITHDRAWAL')
  })

  it('returns an empty array rather than throwing on an empty ledger', () => {
    expect(cashTransfers([])).toEqual([])
  })

  it('leaves the array it was given untouched', () => {
    const original = [...ACCOUNT_SNAPSHOTS.paper.activity]
    cashTransfers(ACCOUNT_SNAPSHOTS.paper.activity)
    expect(ACCOUNT_SNAPSHOTS.paper.activity).toEqual(original)
  })
})

describe('netTransfers', () => {
  it('nets withdrawals against deposits by sign', () => {
    const transfers = cashTransfers(ACCOUNT_SNAPSHOTS.paper.activity)
    const expected = transfers.reduce((sum, t) => sum + (t.amount ?? 0), 0)

    expect(netTransfers(transfers)).toBeCloseTo(expected, 2)
  })

  /** The invariant that matters: `amount` is pre-signed, so a deposit is
   * positive and a withdrawal negative. Inverted, the net reads as money
   * leaving an account that gained it — and because `netTransfers` is a plain
   * sum, nothing downstream would flag it. */
  it.each(MODES)('signs deposits positive and withdrawals negative (%s)', (mode) => {
    for (const t of cashTransfers(ACCOUNT_SNAPSHOTS[mode].activity)) {
      if (t.action === 'DEPOSIT') expect(t.amount).toBeGreaterThan(0)
      if (t.action === 'WITHDRAWAL') expect(t.amount).toBeLessThan(0)
    }
  })

  it('nets to less than the deposits alone, since the paper book has a withdrawal', () => {
    const transfers = cashTransfers(ACCOUNT_SNAPSHOTS.paper.activity)
    const deposits = transfers
      .filter((t) => t.action === 'DEPOSIT')
      .reduce((sum, t) => sum + (t.amount ?? 0), 0)

    expect(netTransfers(transfers)).toBeLessThan(deposits)
  })

  it('is zero for no transfers', () => {
    expect(netTransfers([])).toBe(0)
  })
})

describe('positionsValue', () => {
  it.each(MODES)('sums the market value of every open position (%s)', (mode) => {
    const positions = ACCOUNT_SNAPSHOTS[mode].positions
    const expected = positions.reduce((sum, p) => sum + p.value, 0)

    expect(positionsValue(positions)).toBeCloseTo(expected, 2)
  })

  /** `value` is the contract's price × quantity × 100, not the underlying's.
   * If this ever stops holding, the equity figure on the Account page is
   * quoting the wrong instrument. */
  it.each(MODES)('agrees with last × quantity × multiplier (%s)', (mode) => {
    for (const p of ACCOUNT_SNAPSHOTS[mode].positions) {
      expect(p.value).toBeCloseTo(p.last * p.quantity * CONTRACT_MULTIPLIER, 2)
    }
  })

  it('is zero with no positions, which is a flat book and not an error', () => {
    expect(positionsValue([])).toBe(0)
  })
})

describe('totalEquity', () => {
  it.each(MODES)('is cash plus the value of what is held (%s)', (mode) => {
    const a = ACCOUNT_SNAPSHOTS[mode]
    expect(totalEquity(a.cash, a.positions)).toBeCloseTo(a.cash + positionsValue(a.positions), 2)
  })

  it('equals cash exactly when the book is flat', () => {
    expect(totalEquity(12_480.32, [])).toBeCloseTo(12_480.32, 2)
  })

  /** The two accounts hold different money. If these ever match, something
   * is reading one book while displaying the other — the failure CLAUDE.md
   * calls out for account-scoped state. */
  it('differs between the two accounts', () => {
    const paper = totalEquity(ACCOUNT_SNAPSHOTS.paper.cash, ACCOUNT_SNAPSHOTS.paper.positions)
    const cash = totalEquity(ACCOUNT_SNAPSHOTS.cash.cash, ACCOUNT_SNAPSHOTS.cash.positions)
    expect(paper).not.toBeCloseTo(cash, 2)
  })
})
