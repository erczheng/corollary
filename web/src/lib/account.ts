import { isOrderAction, type ActivityItem, type Position } from './mockData'

/** The Account page's arithmetic, as pure functions with no React in them —
 * the arrangement `orders.ts` and `markets.ts` already use.
 *
 * PRD.md §8.6 is emphatic that **Alpaca is the source of truth and Corollary
 * keeps no ledger**, and nothing here contradicts that: these functions
 * derive figures from what the broker reported, they do not maintain a
 * parallel book. `totalEquity` is a sum of two numbers Alpaca gave us, not a
 * running balance Corollary updates.
 */

/** Cash movements only — deposits and withdrawals, never orders.
 *
 * Filters through `isOrderAction` rather than testing the action strings
 * here, so there is one place that knows a deposit is not a trade. The
 * Dashboard's executions feed makes the same distinction from the other
 * side.
 *
 * Takes the activity of **one** account. There is no merged-book version on
 * purpose: a transfer belongs to the account it landed in, and listing
 * paper's alongside cash's would misstate where real money went. */
export function cashTransfers(activity: ActivityItem[]): ActivityItem[] {
  return activity.filter((item) => !isOrderAction(item.action))
}

/** Deposits net of withdrawals.
 *
 * `amount` is already signed — positive in, negative out — so this is a plain
 * sum. Re-deriving the sign from the action would be a second source of
 * truth about which way the money went. */
export function netTransfers(transfers: ActivityItem[]): number {
  return transfers.reduce((sum, t) => sum + (t.amount ?? 0), 0)
}

/** Market value of everything held.
 *
 * Sums `Position.value`, which is the **contract's** price × quantity × 100 —
 * not the underlying's. The two live in different fields for exactly this
 * reason, and summing `underlying` here would quote a number roughly fifty
 * times too large with no obvious sign that anything was wrong. */
export function positionsValue(positions: Position[]): number {
  return positions.reduce((sum, p) => sum + p.value, 0)
}

/** Cash plus the market value of open positions.
 *
 * `cash` is the account's **total** cash, settled and unsettled together, so
 * this figure includes money that has not cleared. That is the right choice
 * for an equity number — the proceeds are yours, they are simply not
 * spendable yet — but it is not self-evident, so the page says so in words
 * beside it rather than leaving you to work out which reading it took.
 *
 * Ticks with the position stream, since `Position.value` is re-marked on
 * every tick. That is why the page skeletons this line until the first price
 * arrives instead of briefly showing cost basis as though it were equity. */
export function totalEquity(cash: number, positions: Position[]): number {
  return cash + positionsValue(positions)
}
