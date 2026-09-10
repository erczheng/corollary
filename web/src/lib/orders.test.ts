import { describe, it, expect } from 'vitest'
import { ACCOUNT_SNAPSHOTS, MARKET_TODAY, UNDERLYINGS } from './mockData'
import { type Position } from './types'
import {
  MULTI_LEG_NOTE,
  availableOrderTypes,
  breakevens,
  crossingPrice,
  daysToExpiry,
  estimate,
  exitTrigger,
  expiryUrgency,
  expiryWarningDte,
  orderWouldFill,
  DEFAULT_EXPIRY_WARNING_DTE,
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
  estimateOpen,
  occSymbol,
  openCrossingPrice,
  openRisk,
  validateOpenOrder,
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

/** Sells fill at or above their limit and trigger at or below their stop;
 * buys are the mirror. The asymmetry is the whole point of the two order
 * types and is trivially easy to write backwards. */
describe('expiry', () => {
  it('counts calendar days in UTC on both sides', () => {
    expect(daysToExpiry('2026-08-14', '2026-08-07')).toBe(7)
    expect(daysToExpiry('2026-08-07', '2026-08-07')).toBe(0)
    // Past expiries go negative rather than clamping — an expired position
    // is a different state from one expiring today.
    expect(daysToExpiry('2026-08-01', '2026-08-07')).toBe(-6)
  })

  it('crosses a month boundary without drifting', () => {
    expect(daysToExpiry('2026-09-01', '2026-08-31')).toBe(1)
    expect(daysToExpiry('2026-10-17', '2026-08-07')).toBe(71)
  })

  it('uses the strategy’s own time stop as the warning threshold', () => {
    const managed = PAPER.find((p) => p.managedExit !== null)!
    // The point at which the engine would close it anyway is the number
    // that matters for that position.
    expect(expiryWarningDte(managed)).toBe(managed.managedExit!.timeStopDte)

    const detached = PAPER.find((p) => p.managedExit === null)!
    expect(expiryWarningDte(detached)).toBe(DEFAULT_EXPIRY_WARNING_DTE)
  })

  it('separates expired, expiring today, near and normal', () => {
    const p = PAPER[0]
    expect(expiryUrgency({ ...p, expiry: '2026-08-01' }, '2026-08-07')).toBe('expired')
    expect(expiryUrgency({ ...p, expiry: '2026-08-07' }, '2026-08-07')).toBe('today')
    expect(expiryUrgency({ ...p, expiry: '2026-08-08', managedExit: null }, '2026-08-07')).toBe('near')
    expect(expiryUrgency({ ...p, expiry: '2026-12-01', managedExit: null }, '2026-08-07')).toBe('normal')
  })

  it('agrees with the OCC symbol on every leg', () => {
    // OCC format: underlying + YYMMDD + C/P + strike. The expiry field and
    // the symbols have to say the same thing, or the countdown is
    // counting down to a different contract than the one being held.
    for (const p of [...PAPER, ...CASH]) {
      const yymmdd = p.expiry.slice(2).replace(/-/g, '')
      for (const leg of p.legs) {
        expect(leg.symbol).toContain(yymmdd)
        expect(leg.symbol.startsWith(p.symbol)).toBe(true)
      }
    }
  })

  it('keeps at least one position near expiry, so the state is reachable', () => {
    expect([...PAPER, ...CASH].some((p) => expiryUrgency(p, MARKET_TODAY) !== 'normal')).toBe(true)
  })
})

describe('orderWouldFill', () => {
  const sellLimit = { side: 'STC' as const, orderType: 'limit' as const, limitPrice: 3, stopPrice: null }
  const buyLimit = { side: 'BTC' as const, orderType: 'limit' as const, limitPrice: 3, stopPrice: null }
  const sellStop = { side: 'STC' as const, orderType: 'stop' as const, limitPrice: null, stopPrice: 2 }
  const buyStop = { side: 'BTC' as const, orderType: 'stop' as const, limitPrice: null, stopPrice: 4 }

  it('fills a sell limit at or above its price, never below', () => {
    expect(orderWouldFill(sellLimit, 3.5)).toBe(true)
    expect(orderWouldFill(sellLimit, 3)).toBe(true)
    expect(orderWouldFill(sellLimit, 2.99)).toBe(false)
  })

  it('fills a buy limit at or below its price, never above', () => {
    expect(orderWouldFill(buyLimit, 2.5)).toBe(true)
    expect(orderWouldFill(buyLimit, 3)).toBe(true)
    expect(orderWouldFill(buyLimit, 3.01)).toBe(false)
  })

  it('triggers a sell stop on the way down — it is protective', () => {
    expect(orderWouldFill(sellStop, 1.9)).toBe(true)
    expect(orderWouldFill(sellStop, 2.1)).toBe(false)
  })

  it('triggers a buy stop on the way up', () => {
    expect(orderWouldFill(buyStop, 4.1)).toBe(true)
    expect(orderWouldFill(buyStop, 3.9)).toBe(false)
  })
})

describe('exitTrigger', () => {
  const sellExit = { takeProfit: 3, stopPrice: 1.5, stopLimitPrice: null, timeInForce: 'gtc' as const, heldBy: 'broker' as const }
  const buyExit = { takeProfit: 0.2, stopPrice: 1.1, stopLimitPrice: null, timeInForce: 'day' as const, heldBy: 'broker' as const }

  it('takes profit on a long when the mark rises to it', () => {
    expect(exitTrigger(longCall, sellExit, 3.1)).toBe('take_profit')
    expect(exitTrigger(longCall, sellExit, 2)).toBeNull()
    expect(exitTrigger(longCall, sellExit, 1.4)).toBe('stop')
  })

  it('inverts entirely for a short, which exits by buying back', () => {
    // Cheaper is better when you owe it: take profit *below*, stop above.
    expect(exitTrigger(cashShortSpread, buyExit, 0.15)).toBe('take_profit')
    expect(exitTrigger(cashShortSpread, buyExit, 0.6)).toBeNull()
    expect(exitTrigger(cashShortSpread, buyExit, 1.2)).toBe('stop')
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

  it('agrees with the underlying quote every position names', () => {
    // Two positions can share an underlying — SPY backs a paper spread and
    // a cash put. If the quote lived on the position instead of being
    // keyed by symbol, the same stock could show two prices on one page.
    for (const p of all) {
      expect(UNDERLYINGS[p.symbol]).toBeDefined()
      expect(UNDERLYINGS[p.symbol].price).toBe(p.underlying)
    }
  })

  it('ends every underlying series at its current price', () => {
    for (const u of Object.values(UNDERLYINGS)) {
      expect(u.history[u.history.length - 1].value).toBe(u.price)
      // And measures the day from yesterday's close, not from the left
      // edge of a two-month chart.
      expect(u.previousClose).toBe(u.history[u.history.length - 2].value)
      expect(u.change).toBeCloseTo(u.price - u.previousClose, 2)
    }
  })

  it('never puts a weekend on an underlying chart', () => {
    for (const u of Object.values(UNDERLYINGS)) {
      for (const p of u.history) {
        const day = new Date(`${p.date}T00:00:00Z`).getUTCDay()
        expect(day).not.toBe(0)
        expect(day).not.toBe(6)
      }
    }
  })

  it('covers both exit holders and both managed states', () => {
    expect(all.some((p) => p.attachedExit?.heldBy === 'broker')).toBe(true)
    expect(all.some((p) => p.attachedExit?.heldBy === 'corollary')).toBe(true)
    expect(all.some((p) => p.strategyId !== null)).toBe(true)
    expect(all.some((p) => p.strategyId === null)).toBe(true)
  })
})

describe('opening a position from the chain', () => {
  const quote = { bid: 4.9, ask: 5.1 }
  const draft = {
    side: 'BTO' as const,
    quantity: 2,
    orderType: 'market' as const,
    limitPrice: null,
    stopPrice: null,
    timeInForce: 'day' as const,
  }

  it('lifts the ask to buy and hits the bid to sell', () => {
    // The same trap `crossingPrice` documents for closing. Reversed, every
    // buy estimate is understated by the width of the spread.
    expect(openCrossingPrice(quote, 'BTO')).toBe(5.1)
    expect(openCrossingPrice(quote, 'STO')).toBe(4.9)
  })

  it('calls a buy a cost and a sell a credit, never both "proceeds"', () => {
    expect(estimateOpen(quote, draft)).toMatchObject({ kind: 'cost', amount: 1_020 })
    expect(estimateOpen(quote, { ...draft, side: 'STO' })).toMatchObject({
      kind: 'proceeds',
      amount: 980,
    })
  })

  it('fills a limit order at its own price rather than crossing', () => {
    const est = estimateOpen(quote, { ...draft, orderType: 'limit', limitPrice: 4.5 })
    expect(est.pricePerContract).toBe(4.5)
    expect(est.amount).toBe(900)
  })

  it('ignores a blank or zero limit and falls back to the crossing price', () => {
    // A limit of 0 is "not stated yet", not a free order.
    expect(estimateOpen(quote, { ...draft, orderType: 'limit', limitPrice: null }).pricePerContract).toBe(
      5.1,
    )
    expect(estimateOpen(quote, { ...draft, orderType: 'limit', limitPrice: 0 }).pricePerContract).toBe(5.1)
  })

  it('rejects a fractional or missing quantity and a limit order with no limit', () => {
    expect(validateOpenOrder({ ...draft, quantity: 1.5 })).toHaveLength(1)
    expect(validateOpenOrder({ ...draft, quantity: 0 })).toHaveLength(1)
    expect(validateOpenOrder({ ...draft, orderType: 'limit', limitPrice: null })).toHaveLength(1)
    expect(validateOpenOrder(draft)).toEqual([])
  })
})

describe('openRisk', () => {
  const quote = { bid: 4.9, ask: 5.1 }
  const draft = {
    side: 'BTO' as const,
    quantity: 2,
    orderType: 'market' as const,
    limitPrice: null,
    stopPrice: null,
    timeInForce: 'day' as const,
  }

  it('risks the premium paid on a long, and states it as a percent of equity', () => {
    // CLAUDE.md rule 4: for a long option, risk *is* the premium.
    expect(openRisk(quote, draft, 34_000)).toEqual({ kind: 'defined', amount: 1_020, pct: 3 })
  })

  it('refuses to quote a maximum loss on a short', () => {
    // A naked short is undefined risk, sized against a ±2σ stress loss the
    // engine computes. A confident wrong number under the word "risk" is
    // worse than an honest absence.
    expect(openRisk(quote, { ...draft, side: 'STO' }, 34_000)).toEqual({ kind: 'undefined' })
  })

  it('reports zero percent rather than dividing by an empty account', () => {
    expect(openRisk(quote, draft, 0)).toEqual({ kind: 'defined', amount: 1_020, pct: 0 })
  })
})

describe('occSymbol', () => {
  it('builds the OCC format the docs specify', () => {
    // From CLAUDE.md: AAPL241220C00150000 is the AAPL $150 call expiring
    // 20 Dec 2024.
    expect(occSymbol('AAPL', '2024-12-20', 'call', 150)).toBe('AAPL241220C00150000')
  })

  it('multiplies the strike by a thousand and pads to eight digits', () => {
    // The ×1000 and the pad are both load-bearing — $7.50 written as
    // 00000750 is a 75-cent strike.
    expect(occSymbol('SPY', '2026-08-21', 'put', 7.5)).toBe('SPY260821P00007500')
    expect(occSymbol('SPY', '2026-08-21', 'put', 430)).toBe('SPY260821P00430000')
    expect(occSymbol('COST', '2026-10-16', 'call', 884.19)).toBe('COST261016C00884190')
  })
})
