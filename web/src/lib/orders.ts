/**
 * Every rule about an order that could be wrong about money, as pure
 * functions with no React in them. They live here rather than inside the
 * ticket component so they can be tested without rendering anything, and
 * so the engine can eventually share them.
 *
 * Nothing in this file places an order. In Phase 2 the submit path runs
 * through `RiskManager.approve()` and nowhere else (CLAUDE.md rule 1).
 */

import { CONTRACT_MULTIPLIER } from './mockData'
import {
  type AttachedExit,
  type OrderSide,
  type OrderType,
  type Position,
  type PositionLeg,
  type TimeInForce,
} from './types'

const round2 = (n: number) => Math.round(n * 100) / 100

export type TicketMode = 'close' | 'add' | 'exit'

/** BTO/STC/STO/BTC are what the industry calls these and what the fill
 * comes back as — not abbreviations to be helpfully expanded away. The
 * type itself lives in mockData so `WorkingOrder` can use it without the
 * two modules importing each other. */
export type { OrderSide }

/** A market order fills. Anything else sits and works until it does, or
 * until you cancel it. */
export function isWorkingOrderType(type: OrderType): type is Exclude<OrderType, 'market'> {
  return type !== 'market'
}

export const ORDER_SIDE_LABEL: Record<OrderSide, string> = {
  BTO: 'Buy to open (BTO)',
  STC: 'Sell to close (STC)',
  STO: 'Sell to open (STO)',
  BTC: 'Buy to close (BTC)',
}

export function isMultiLeg(position: Position): boolean {
  return position.legs.length > 1
}

/** Alpaca documents multi-leg orders as limit only, and Level 3 rejects an
 * MLeg order whose legs are not all covered within the same order. A
 * market order on a spread is a bad idea regardless of what the API
 * permits — you cross two spreads at once and find out afterwards. */
export const MULTI_LEG_NOTE =
  'Multi-leg positions accept limit orders only — a market order on a spread crosses both legs at once.'

export function availableOrderTypes(position: Position): OrderType[] {
  return isMultiLeg(position) ? ['limit'] : ['market', 'limit', 'stop', 'stop_limit']
}

/** Adding to a short is a *sell* to open. Labelling that button "Buy"
 * would name the opposite of the order it places, which is why the ticket
 * shows the resolved side rather than assuming buy-means-open. */
export function resolvedSide(position: Position, mode: 'close' | 'add'): OrderSide {
  if (mode === 'close') return position.direction === 'long' ? 'STC' : 'BTC'
  return position.direction === 'long' ? 'BTO' : 'STO'
}

export function isSelling(side: OrderSide): boolean {
  return side === 'STC' || side === 'STO'
}

/** Selling hits the bid, buying lifts the ask. Getting this backwards
 * overstates proceeds by the width of the spread on every estimate. */
export function crossingPrice(position: Position, mode: 'close' | 'add'): number {
  return isSelling(resolvedSide(position, mode)) ? position.bid : position.ask
}

export function midPrice(quote: { bid: number; ask: number }): number {
  return round2((quote.bid + quote.ask) / 2)
}

export interface OrderDraft {
  mode: 'close' | 'add'
  quantity: number
  orderType: OrderType
  limitPrice: number | null
  stopPrice: number | null
  timeInForce: TimeInForce
}

export interface Estimate {
  /** Proceeds on a sell, cost on a buy. Never both under one word — a
   * debit shown as "proceeds" reads as money coming in. */
  kind: 'proceeds' | 'cost'
  amount: number
  pricePerContract: number
  side: OrderSide
}

export function estimate(position: Position, draft: OrderDraft): Estimate {
  const side = resolvedSide(position, draft.mode)
  // A market order, and a stop once it triggers, both cross the spread.
  // A limit or stop-limit fills at its own price or not at all.
  const usesLimit = draft.orderType === 'limit' || draft.orderType === 'stop_limit'
  const pricePerContract =
    usesLimit && draft.limitPrice !== null && draft.limitPrice > 0
      ? draft.limitPrice
      : crossingPrice(position, draft.mode)

  return {
    kind: isSelling(side) ? 'proceeds' : 'cost',
    amount: round2(pricePerContract * draft.quantity * CONTRACT_MULTIPLIER),
    pricePerContract,
    side,
  }
}

export function validateOrder(position: Position, draft: OrderDraft): string[] {
  const errors: string[] = []

  if (!Number.isInteger(draft.quantity) || draft.quantity < 1) {
    errors.push('Quantity must be a whole number of contracts, at least 1.')
  } else if (draft.mode === 'close' && draft.quantity > position.quantity) {
    errors.push(`You hold ${position.quantity} contract${position.quantity === 1 ? '' : 's'} — cannot close more.`)
  }

  if (!availableOrderTypes(position).includes(draft.orderType)) {
    errors.push(MULTI_LEG_NOTE)
  }

  const needsLimit = draft.orderType === 'limit' || draft.orderType === 'stop_limit'
  const needsStop = draft.orderType === 'stop' || draft.orderType === 'stop_limit'

  if (needsLimit && !(draft.limitPrice !== null && draft.limitPrice > 0)) {
    errors.push('Limit price is required.')
  }
  if (needsStop && !(draft.stopPrice !== null && draft.stopPrice > 0)) {
    errors.push('Stop price is required.')
  }

  return errors
}

// -------------------------------------------------------------------- //
// Attached exits — modelled on Alpaca's order_class: oco
// -------------------------------------------------------------------- //

export interface ExitDraft {
  takeProfit: number | null
  stopPrice: number | null
  stopLimitPrice: number | null
  timeInForce: TimeInForce
}

/** Alpaca rejects an OCO whose stop price is not at least a cent clear of
 * its base price. The base is the take-profit limit, and separately the
 * current mark. The restriction exists to avoid a race in order handling,
 * so it is a real broker rule and not a nicety we could round away. */
export const STOP_THRESHOLD = 0.01

/** A long position is exited by selling, a short by buying back — and the
 * threshold inverts with it. On a sell the stop sits *below* the
 * take-profit; on a buy-to-close it sits *above*. */
export function exitIsSell(position: Position): boolean {
  return position.direction === 'long'
}

export function validateExit(position: Position, draft: ExitDraft): string[] {
  const errors: string[] = []
  const { takeProfit, stopPrice, stopLimitPrice } = draft

  if (!(takeProfit !== null && takeProfit > 0)) errors.push('Take-profit price is required.')
  if (!(stopPrice !== null && stopPrice > 0)) errors.push('Stop price is required.')
  if (takeProfit === null || stopPrice === null || takeProfit <= 0 || stopPrice <= 0) return errors

  const sell = exitIsSell(position)
  const mark = position.last

  if (sell) {
    if (stopPrice > takeProfit - STOP_THRESHOLD) {
      errors.push('Stop must be at least $0.01 below the take-profit price.')
    }
    if (stopPrice > mark - STOP_THRESHOLD) {
      errors.push('Stop must be at least $0.01 below the current mark, or it would trigger immediately.')
    }
    if (stopLimitPrice !== null && stopLimitPrice > stopPrice) {
      errors.push('Stop limit must be at or below the stop price, or the stop may never fill.')
    }
  } else {
    if (stopPrice < takeProfit + STOP_THRESHOLD) {
      errors.push('Stop must be at least $0.01 above the take-profit price.')
    }
    if (stopPrice < mark + STOP_THRESHOLD) {
      errors.push('Stop must be at least $0.01 above the current mark, or it would trigger immediately.')
    }
    if (stopLimitPrice !== null && stopLimitPrice < stopPrice) {
      errors.push('Stop limit must be at or above the stop price, or the stop may never fill.')
    }
  }

  return errors
}

// -------------------------------------------------------------------- //
// Expiry
// -------------------------------------------------------------------- //

/** Calendar days from `today` to `expiry`, both date-only.
 *
 * Parsed as UTC on both sides. A bare `YYYY-MM-DD` is UTC midnight, so
 * mixing in a local-time `new Date()` would put the two an offset apart
 * and produce an off-by-one on any afternoon in New York — the same trap
 * `formatExpiry` documents. */
export function daysToExpiry(expiry: string, today: string): number {
  const ms = Date.parse(`${expiry}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)
  return Math.round(ms / 86_400_000)
}

/** How close to expiry counts as close.
 *
 * A position managed by a strategy uses that strategy's own `time_stop_dte`
 * (PRD.md §5.1) — the point at which the engine would close it anyway, so
 * it is the number that actually matters for that position. Anything
 * detached falls back to a week, which is where gamma starts to bite. */
export const DEFAULT_EXPIRY_WARNING_DTE = 7

export function expiryWarningDte(position: Position): number {
  return position.managedExit?.timeStopDte ?? DEFAULT_EXPIRY_WARNING_DTE
}

export type ExpiryUrgency = 'expired' | 'today' | 'near' | 'normal'

export function expiryUrgency(position: Position, today: string): ExpiryUrgency {
  const dte = daysToExpiry(position.expiry, today)
  if (dte < 0) return 'expired'
  if (dte === 0) return 'today'
  return dte <= expiryWarningDte(position) ? 'near' : 'normal'
}

// -------------------------------------------------------------------- //
// What a moving price does to a resting order
// -------------------------------------------------------------------- //

/** Whether a working order would fill at `price`.
 *
 * Sells fill at or above their limit and trigger at or below their stop;
 * buys are the mirror. The asymmetry is the whole point of the two order
 * types and is easy to write backwards, which is why it is one function
 * with a test rather than a condition inlined at the call site.
 *
 * A stop-limit is treated as filling when its *stop* is touched. That is a
 * simplification: in a real market the limit can then go unfilled, and
 * `backtest/spread.py` will have to model that properly. Phase 1 has no
 * order book to miss against. */
export function orderWouldFill(
  order: { side: OrderSide; orderType: Exclude<OrderType, 'market'>; limitPrice: number | null; stopPrice: number | null },
  price: number,
): boolean {
  const selling = isSelling(order.side)

  if (order.orderType === 'limit') {
    if (order.limitPrice === null) return false
    return selling ? price >= order.limitPrice : price <= order.limitPrice
  }

  if (order.stopPrice === null) return false
  // A sell stop is protective: it triggers when the price falls to it.
  return selling ? price <= order.stopPrice : price >= order.stopPrice
}

export type ExitTrigger = 'take_profit' | 'stop'

/** Which leg of an attached exit, if either, `price` has reached.
 *
 * Take-profit is checked first. When a single tick jumps past both — a gap
 * through the whole range — filling at the favourable one is the wrong
 * assumption to bake in silently, so this is stated: the take-profit wins
 * ties here, and a real broker would fill whichever the market touched
 * first. Phase 2 gets this from the fill, not from a guess. */
export function exitTrigger(position: Position, exit: AttachedExit, price: number): ExitTrigger | null {
  const selling = exitIsSell(position)

  if (selling) {
    if (price >= exit.takeProfit) return 'take_profit'
    if (price <= exit.stopPrice) return 'stop'
  } else {
    if (price <= exit.takeProfit) return 'take_profit'
    if (price >= exit.stopPrice) return 'stop'
  }
  return null
}

// -------------------------------------------------------------------- //
// Payoff at expiry
// -------------------------------------------------------------------- //

export interface PayoffPoint {
  underlying: number
  pnl: number
}

function intrinsic(leg: PositionLeg, underlying: number): number {
  return leg.right === 'call'
    ? Math.max(underlying - leg.strike, 0)
    : Math.max(leg.strike - underlying, 0)
}

/** What the structure is worth per unit at expiry, at a given underlying
 * price. Long legs contribute their intrinsic value; short legs subtract
 * it, because you owe them. */
export function structureValue(position: Position, underlying: number): number {
  return position.legs.reduce(
    (total, leg) => total + leg.ratio * (leg.side === 'long' ? 1 : -1) * intrinsic(leg, underlying),
    0,
  )
}

/** What the position was worth per unit when it was opened, signed the
 * same way `structureValue` is. A long paid a debit and holds an asset; a
 * short took in a credit and holds a liability, so its open value is
 * negative. Without the sign, every short's payoff curve is inverted. */
export function openUnitValue(position: Position): number {
  const perUnit = position.costBasis / (position.quantity * CONTRACT_MULTIPLIER)
  return position.direction === 'long' ? perUnit : -perUnit
}

export function payoffAt(position: Position, underlying: number): number {
  const perUnit = structureValue(position, underlying) - openUnitValue(position)
  return round2(perUnit * position.quantity * CONTRACT_MULTIPLIER)
}

/** Spans every strike plus the current underlying, with room either side
 * so the flat regions of a defined-risk structure are visible rather than
 * clipped at the edge of the plot. */
export function payoffCurve(position: Position, steps = 81): PayoffPoint[] {
  const marks = [...position.legs.map((l) => l.strike), position.underlying]
  const lo = Math.min(...marks) * 0.88
  const hi = Math.max(...marks) * 1.12
  const step = (hi - lo) / (steps - 1)

  return Array.from({ length: steps }, (_, i) => {
    const underlying = round2(lo + step * i)
    return { underlying, pnl: payoffAt(position, underlying) }
  })
}

/** Underlying prices where the curve crosses zero, found by linear
 * interpolation across a sign change. Numeric rather than closed-form so
 * it works for any structure, including ones with more legs than the
 * fixtures currently carry. */
export function breakevens(curve: PayoffPoint[]): number[] {
  const out: number[] = []
  for (let i = 1; i < curve.length; i++) {
    const a = curve[i - 1]
    const b = curve[i]
    if (a.pnl === 0) out.push(a.underlying)
    else if (a.pnl < 0 !== b.pnl < 0) {
      const t = Math.abs(a.pnl) / (Math.abs(a.pnl) + Math.abs(b.pnl))
      out.push(round2(a.underlying + (b.underlying - a.underlying) * t))
    }
  }
  return out
}

export function maxProfit(curve: PayoffPoint[]): number {
  return Math.max(...curve.map((p) => p.pnl))
}

export function maxLoss(curve: PayoffPoint[]): number {
  return Math.min(...curve.map((p) => p.pnl))
}

// -------------------------------------------------------------------- //
// Risk limits — advisory only
// -------------------------------------------------------------------- //

/** What adding to this position would risk, as a percentage of equity.
 *
 * This is an **estimate for display**, never an approval. CLAUDE.md rule 4
 * is explicit that the engine enforces limits and a value arriving from
 * the client is never trusted; the UI's job here is to make a rejection
 * unsurprising, not to pre-empt one. The ticket renders this beside the
 * ceiling and lets you submit either way — the risk manager decides. */
export function addedRiskPct(position: Position, quantity: number, equity: number): number {
  if (equity <= 0) return 0
  const perUnit = Math.abs(openUnitValue(position))
  return round2(((perUnit * quantity * CONTRACT_MULTIPLIER) / equity) * 100)
}

// -------------------------------------------------------------------- //
// Opening a position from a chain row (Markets)
// -------------------------------------------------------------------- //

/** The two numbers any order estimate needs. A `Position` satisfies it, and
 * so does an `OptionContract` — the bid/ask rules are the same whether you
 * are closing something you hold or opening something you don't, and
 * writing them twice is how the two drift apart. */
export interface Quote {
  bid: number
  ask: number
}

/** Opening is a choice of side, not a consequence of one. A position you
 * hold has a direction that decides how it closes; a contract on the chain
 * has none until you pick it. */
export type OpenSide = Extract<OrderSide, 'BTO' | 'STO'>

export const OPEN_SIDES: OpenSide[] = ['BTO', 'STO']

/** Buying to open lifts the ask, selling to open hits the bid. Reversing
 * this understates the cost of every buy by the width of the spread —
 * the same trap `crossingPrice` documents for closing. */
export function openCrossingPrice(quote: Quote, side: OpenSide): number {
  return side === 'BTO' ? quote.ask : quote.bid
}

export interface OpenDraft {
  side: OpenSide
  quantity: number
  orderType: OrderType
  limitPrice: number | null
  stopPrice: number | null
  timeInForce: TimeInForce
}

export function estimateOpen(quote: Quote, draft: OpenDraft): Estimate {
  const usesLimit = draft.orderType === 'limit' || draft.orderType === 'stop_limit'
  const pricePerContract =
    usesLimit && draft.limitPrice !== null && draft.limitPrice > 0
      ? draft.limitPrice
      : openCrossingPrice(quote, draft.side)

  return {
    kind: isSelling(draft.side) ? 'proceeds' : 'cost',
    amount: round2(pricePerContract * draft.quantity * CONTRACT_MULTIPLIER),
    pricePerContract,
    side: draft.side,
  }
}

export function validateOpenOrder(draft: OpenDraft): string[] {
  const errors: string[] = []

  if (!Number.isInteger(draft.quantity) || draft.quantity < 1) {
    errors.push('Quantity must be a whole number of contracts, at least 1.')
  }

  const needsLimit = draft.orderType === 'limit' || draft.orderType === 'stop_limit'
  const needsStop = draft.orderType === 'stop' || draft.orderType === 'stop_limit'

  if (needsLimit && !(draft.limitPrice !== null && draft.limitPrice > 0)) {
    errors.push('Limit price is required.')
  }
  if (needsStop && !(draft.stopPrice !== null && draft.stopPrice > 0)) {
    errors.push('Stop price is required.')
  }

  return errors
}

/** What this order puts at risk, in the terms CLAUDE.md rule 4 defines.
 *
 * A long option risks the premium paid, and nothing else — that is a number
 * the UI can state exactly. A naked short is **undefined risk**, and the
 * limit is checked against a stress loss at ±2σ of the underlying's 20-day
 * realized volatility. That is an engine computation over data this page
 * does not have, so the ticket says so rather than inventing a figure: a
 * confident wrong number under a "risk" label is worse than an honest
 * absence, and either way the engine is what enforces the ceiling
 * (CLAUDE.md rule 4 — the UI displays limits, it never decides them). */
export type OpenRisk =
  | { kind: 'defined'; amount: number; pct: number }
  | { kind: 'undefined' }

export function openRisk(quote: Quote, draft: OpenDraft, equity: number): OpenRisk {
  if (draft.side === 'STO') return { kind: 'undefined' }

  const { pricePerContract } = estimateOpen(quote, draft)
  const amount = round2(pricePerContract * draft.quantity * CONTRACT_MULTIPLIER)
  return {
    kind: 'defined',
    amount,
    pct: equity <= 0 ? 0 : round2((amount / equity) * 100),
  }
}

/** OCC symbol: underlying, then YYMMDD, then C or P, then the strike times
 * a thousand padded to eight digits. `AAPL241220C00150000` is the AAPL $150
 * call expiring 20 Dec 2024.
 *
 * The ×1000 and the pad are both load-bearing — a $150 strike written as
 * `00150` or `150000000` is a different contract or no contract at all.
 * Adjusted contracts carry a numeric root suffix (`AAPL1`) and a deliverable
 * that is no longer 100 shares; this does not build those, and the scanner
 * filters them out rather than sizing them wrong. */
export function occSymbol(
  underlying: string,
  expiration: string,
  right: 'call' | 'put',
  strike: number,
): string {
  const [year, month, day] = expiration.split('-')
  const strikeThousandths = String(Math.round(strike * 1000)).padStart(8, '0')
  return `${underlying}${year.slice(2)}${month}${day}${right === 'call' ? 'C' : 'P'}${strikeThousandths}`
}
