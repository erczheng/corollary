import { describe, it, expect } from 'vitest'
import { ACCOUNT_SNAPSHOTS, type Position } from './mockData'
import {
  MULTI_LEG_NOTE,
  availableOrderTypes,
  breakevens,
  crossingPrice,
  estimate,
  isMultiLeg,
  maxLoss,
  maxProfit,
  midPrice,
  openUnitValue,
  payoffAt,
  payoffCurve,
  resolvedSide,
  structureValue,
  validateExit,
  validateOrder,
  addedRiskPct,
} from './orders'

const PAPER = ACCOUNT_SNAPSHOTS.paper.positions
const CASH = ACCOUNT_SNAPSHOTS.cash.positions

const longCall = PAPER.find((p) => p.id === 'pos-1')!
const longPut = PAPER.find((p) => p.id === 'pos-2')!
const shortSpread = PAPER.find((p) => p.id === 'pos-3')!
const cashShortSpread = CASH.find((p) => p.id === 'cash-pos-2')!

const draft = (over: Partial<Parameters<typeof estimate>[1]> = {}) => ({
  mode: 'close' as const,
  quantity: 1,
  orderType: 'market' as const,
  limitPrice: null,
  stopPrice: null,
  timeInForce: 'day' as const,
  ...over,
})

describe('order types available', () => {
  it('offers all four on a single-leg position', () => {
    expect(isMultiLeg(longCall)).toBe(false)
    expect(availableOrderTypes(longCall)).toEqual(['market', 'limit', 'stop', 'stop_limit'])
  })

  it('offers limit alone on a multi-leg position', () => {
    expect(isMultiLeg(shortSpread)).toBe(true)
    expect(availableOrderTypes(shortSpread)).toEqual(['limit'])
  })

  it('rejects an order that asks for a type the position cannot take', () => {
    const errors = validateOrder(shortSpread, draft({ orderType: 'market' }))
    expect(errors).toContain(MULTI_LEG_NOTE)
  })
})

/** Adding to a short is a sell. A button reading "Buy" on the SPY credit
 * spread would name the opposite of the order it places. */
describe('resolved side', () => {
  it('closes a long by selling and a short by buying back', () => {
    expect(resolvedSide(longCall, 'close')).toBe('STC')
    expect(resolvedSide(shortSpread, 'close')).toBe('BTC')
  })

  it('adds to a long by buying to open and to a short by selling to open', () => {
    expect(resolvedSide(longCall, 'add')).toBe('BTO')
    expect(resolvedSide(shortSpread, 'add')).toBe('STO')
  })
})

describe('crossing the spread', () => {
  it('sells at the bid and buys at the ask', () => {
    expect(crossingPrice(longCall, 'close')).toBe(longCall.bid)
    expect(crossingPrice(longCall, 'add')).toBe(longCall.ask)
    expect(crossingPrice(shortSpread, 'close')).toBe(shortSpread.ask)
    expect(crossingPrice(shortSpread, 'add')).toBe(shortSpread.bid)
  })

  it('puts mid between the two', () => {
    expect(midPrice(longCall)).toBe(2.06)
  })
})

describe('estimate', () => {
  it('reports proceeds on a close of a long, priced at the bid', () => {
    const e = estimate(longCall, draft({ quantity: 2 }))
    expect(e.kind).toBe('proceeds')
    expect(e.pricePerContract).toBe(longCall.bid)
    expect(e.amount).toBe(longCall.bid * 2 * 100)
  })

  it('reports a cost on a close of a short, priced at the ask', () => {
    const e = estimate(shortSpread, draft({ quantity: 3, orderType: 'limit', limitPrice: null }))
    // A debit rendered under the word "proceeds" reads as money coming in.
    expect(e.kind).toBe('cost')
    expect(e.pricePerContract).toBe(shortSpread.ask)
  })

  it('uses the limit price where one applies, not the crossing price', () => {
    const e = estimate(longCall, draft({ quantity: 1, orderType: 'limit', limitPrice: 2.5 }))
    expect(e.pricePerContract).toBe(2.5)
    expect(e.amount).toBe(250)
  })

  it('falls back to the crossing price for a market or stop order', () => {
    const stop = estimate(longCall, draft({ orderType: 'stop', stopPrice: 1.5, limitPrice: 9.99 }))
    // A stop becomes a market order when it triggers; the limit field is
    // not in play, so an estimate built from it would be fiction.
    expect(stop.pricePerContract).toBe(longCall.bid)
  })
})

describe('order validation', () => {
  it('will not close more contracts than are held', () => {
    expect(validateOrder(longCall, draft({ quantity: 3 }))).toContainEqual(
      expect.stringContaining('cannot close more'),
    )
    expect(validateOrder(longCall, draft({ quantity: 2 }))).toEqual([])
  })

  it('allows adding beyond the held quantity, which is the point of adding', () => {
    expect(validateOrder(longCall, draft({ mode: 'add', quantity: 10 }))).toEqual([])
  })

  it('requires the prices the chosen type actually uses', () => {
    expect(validateOrder(longCall, draft({ orderType: 'limit' }))).toContainEqual(
      expect.stringContaining('Limit price is required'),
    )
    expect(validateOrder(longCall, draft({ orderType: 'stop' }))).toContainEqual(
      expect.stringContaining('Stop price is required'),
    )
    expect(
      validateOrder(longCall, draft({ orderType: 'stop_limit', limitPrice: 2, stopPrice: 2.1 })),
    ).toEqual([])
  })

  it('rejects a fractional or zero quantity', () => {
    expect(validateOrder(longCall, draft({ quantity: 0 })).length).toBeGreaterThan(0)
    expect(validateOrder(longCall, draft({ quantity: 1.5 })).length).toBeGreaterThan(0)
  })
})

/** Alpaca rejects an OCO whose stop is not a cent clear of its base price,
 * and the direction of "clear" inverts with the side of the exit. */
describe('exit validation', () => {
  it('accepts a sell-side exit with the stop a cent below both bases', () => {
    // longCall marks at 2.06.
    expect(
      validateExit(longCall, { takeProfit: 3.0, stopPrice: 1.5, stopLimitPrice: null, timeInForce: 'gtc' }),
    ).toEqual([])
  })

  it('rejects a sell-side stop that is not below the take-profit', () => {
    expect(
      validateExit(longCall, { takeProfit: 3.0, stopPrice: 3.0, stopLimitPrice: null, timeInForce: 'gtc' }),
    ).toContainEqual(expect.stringContaining('below the take-profit'))
  })

  it('rejects a sell-side stop at or above the current mark', () => {
    expect(
      validateExit(longCall, { takeProfit: 5.0, stopPrice: 2.06, stopLimitPrice: null, timeInForce: 'gtc' }),
    ).toContainEqual(expect.stringContaining('below the current mark'))
  })

  it('inverts the whole rule for a short, which exits by buying back', () => {
    // cashShortSpread marks at 0.49. Take profit is buying back cheaper;
    // the stop is buying back dearer, so it sits above.
    expect(
      validateExit(cashShortSpread, {
        takeProfit: 0.2,
        stopPrice: 1.1,
        stopLimitPrice: null,
        timeInForce: 'day',
      }),
    ).toEqual([])

    // The arrangement that is valid for a long is invalid here.
    expect(
      validateExit(cashShortSpread, {
        takeProfit: 1.1,
        stopPrice: 0.2,
        stopLimitPrice: null,
        timeInForce: 'day',
      }).length,
    ).toBeGreaterThan(0)
  })

  it('keeps a stop-limit on the fillable side of its stop', () => {
    expect(
      validateExit(longCall, { takeProfit: 3.0, stopPrice: 1.5, stopLimitPrice: 1.9, timeInForce: 'gtc' }),
    ).toContainEqual(expect.stringContaining('at or below the stop price'))
    expect(
      validateExit(longCall, { takeProfit: 3.0, stopPrice: 1.5, stopLimitPrice: 1.4, timeInForce: 'gtc' }),
    ).toEqual([])
  })

  it('requires both prices', () => {
    const errors = validateExit(longCall, {
      takeProfit: null,
      stopPrice: null,
      stopLimitPrice: null,
      timeInForce: 'day',
    })
    expect(errors).toHaveLength(2)
  })
})

describe('payoff at expiry', () => {
  it('caps a long call loss at the premium paid', () => {
    // 350 paid over 2 contracts. Far below the strike, the call expires
    // worthless and the loss is exactly what it cost.
    expect(payoffAt(longCall, 200)).toBe(-350)
  })

  it('runs a long call profit linearly above the strike', () => {
    // openUnitValue is 1.75; at 240 the call is worth 10.
    expect(openUnitValue(longCall)).toBe(1.75)
    expect(payoffAt(longCall, 240)).toBe((10 - 1.75) * 2 * 100)
  })

  it('caps a credit spread at the credit received and the width less credit', () => {
    const curve = payoffCurve(shortSpread)
    // 0.70 credit on a $5-wide spread, 3 contracts.
    expect(openUnitValue(shortSpread)).toBe(-0.7)
    expect(maxProfit(curve)).toBe(210)
    expect(maxLoss(curve)).toBe(-1290)
  })

  it('subtracts a short leg rather than adding it', () => {
    // Both legs in the money: short 430 put owes 10, long 425 put is worth 5.
    expect(structureValue(shortSpread, 420)).toBe(-5)
    // Both worthless above the strikes.
    expect(structureValue(shortSpread, 440)).toBe(0)
  })

  it('finds the breakeven where the curve crosses zero', () => {
    // A credit spread breaks even at the short strike less the credit.
    const [be] = breakevens(payoffCurve(shortSpread))
    expect(be).toBeGreaterThan(429)
    expect(be).toBeLessThan(429.5)
  })

  it('spans every strike and the current underlying', () => {
    const curve = payoffCurve(shortSpread)
    const lows = curve[0].underlying
    const highs = curve[curve.length - 1].underlying
    for (const leg of shortSpread.legs) {
      expect(leg.strike).toBeGreaterThan(lows)
      expect(leg.strike).toBeLessThan(highs)
    }
    expect(shortSpread.underlying).toBeGreaterThan(lows)
    expect(shortSpread.underlying).toBeLessThan(highs)
  })

  it('is direction-aware — a long put profits as the underlying falls', () => {
    expect(payoffAt(longPut, 200)).toBeGreaterThan(0)
    expect(payoffAt(longPut, 280)).toBeLessThan(0)
  })
})

describe('added risk is advisory', () => {
  it('scales with quantity against equity', () => {
    // 1.75 per unit × 2 contracts × 100 = 350, against 35,000 equity.
    expect(addedRiskPct(longCall, 2, 35_000)).toBe(1)
  })

  it('never divides by a zero or negative equity', () => {
    expect(addedRiskPct(longCall, 2, 0)).toBe(0)
  })
})

describe('fixtures are internally consistent', () => {
  const all: Position[] = [...PAPER, ...CASH]

  it('marks every position inside its own spread', () => {
    // The assertion that would have caught `last` holding the underlying's
    // price while bid/ask held the contract's.
    for (const p of all) {
      expect(p.last).toBeGreaterThanOrEqual(p.bid)
      expect(p.last).toBeLessThanOrEqual(p.ask)
    }
  })

  it('derives value from the mark, quantity and multiplier', () => {
    for (const p of all) {
      expect(p.value).toBeCloseTo(p.last * p.quantity * 100, 2)
    }
  })

  it('runs P&L the right way for the direction', () => {
    for (const p of all) {
      const expected = p.direction === 'long' ? p.value - p.costBasis : p.costBasis - p.value
      expect(p.pnl).toBeCloseTo(expected, 2)
    }
  })

  it('starts every value history at cost basis and ends it at current value', () => {
    for (const p of all) {
      expect(p.valueHistory[0].value).toBe(p.costBasis)
      expect(p.valueHistory[p.valueHistory.length - 1].value).toBe(p.value)
    }
  })

  it('covers both exit holders and both managed states', () => {
    expect(all.some((p) => p.attachedExit?.heldBy === 'broker')).toBe(true)
    expect(all.some((p) => p.attachedExit?.heldBy === 'corollary')).toBe(true)
    expect(all.some((p) => p.strategyId !== null)).toBe(true)
    expect(all.some((p) => p.strategyId === null)).toBe(true)
  })
})
