/**
 * Mock data for Phase 1 — Shell. Every shape here is written to match what
 * the real API will eventually return (see PRD.md §8 for the page specs
 * these back), so swapping a page from this file to a TanStack Query hook
 * in Phase 2 is a data-source change, not a component rewrite.
 *
 * Deterministic on purpose — a seeded PRNG, not Math.random() — so
 * screenshots and any future component tests don't flake between runs.
 */

// ---------------------------------------------------------------------- //
// Seeded PRNG (mulberry32) — small, deterministic, no dependency.
// ---------------------------------------------------------------------- //

export function mulberry32(seed: number) {
  let a = seed
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const rand = mulberry32(20260807)

// ---------------------------------------------------------------------- //
// Portfolio performance history (Dashboard)
// ---------------------------------------------------------------------- //

/** Which Alpaca account's keys are in use. Defined here rather than in the
 * UI store because it keys the fixtures below; the store imports it back.
 * In Phase 2 this keys the API request instead. */
export type AccountMode = 'paper' | 'cash'

/** The session every fixture is written relative to.
 *
 * Days-to-expiry is measured from here rather than from the real clock, so
 * a fixture doesn't rot: tied to `new Date()`, a position written five
 * days from expiry silently becomes an expired one next week and the
 * near-expiry state stops being reachable. Phase 2 replaces this with the
 * market calendar's today. */
export const MARKET_TODAY = '2026-08-07'

export interface PricePoint {
  date: string // ISO date
  value: number
}

/** One year of daily closes. `next` is the PRNG draw, passed in so a series
 * can either share the module-level stream or run on its own seed. The
 * paper and benchmark series must keep drawing from `rand` in this order or
 * their values shift. First run was 2025-08-08 per Corollary's rule of
 * seeding at t0 — no pre-Corollary reconstruction. */
function buildSeries(
  next: () => number,
  startValue: number,
  drift: number,
  noiseSpread: number,
  noiseCenter: number,
): PricePoint[] {
  const points: PricePoint[] = []
  let value = startValue
  const start = new Date('2025-08-08T00:00:00Z')
  for (let i = 0; i < 365; i++) {
    const date = new Date(start)
    date.setUTCDate(date.getUTCDate() + i)
    const day = date.getUTCDay()
    if (day === 0 || day === 6) continue // market closed weekends
    const noise = (next() - noiseCenter) * noiseSpread
    value = value * (1 + drift + noise)
    points.push({ date: date.toISOString().slice(0, 10), value: Math.round(value * 100) / 100 })
  }
  return points
}

/** Paper and the SPY benchmark are indexed to the same starting value so
 * the overlay is visually comparable. */
const PAPER_HISTORY = buildSeries(rand, 25_000, 0.0006, 0.018, 0.48)

export const BENCHMARK_HISTORY: PricePoint[] = buildSeries(rand, 25_000, 0.00035, 0.01, 0.5)

/** Cash runs on its own seed so that adding it left the paper series
 * byte-identical. Smaller and calmer than paper — it's real money, sized
 * accordingly. */
const CASH_HISTORY = buildSeries(mulberry32(20260808), 8_000, 0.00042, 0.013, 0.485)

export type ChartRange = '1D' | '1W' | '1M' | '3M' | 'YTD' | '1Y' | 'All'

export function sliceRange(points: PricePoint[], range: ChartRange): PricePoint[] {
  if (range === 'All') return points
  // This mock series is daily closes only, so "1D" has no intraday shape
  // to show — the real provider will supply intraday bars in Phase 2.
  // Two points keeps the line renderable instead of an empty chart.
  if (range === '1D') return points.slice(-2)
  const last = new Date(points[points.length - 1].date)
  const cutoff = new Date(last)
  switch (range) {
    case '1W':
      cutoff.setUTCDate(cutoff.getUTCDate() - 7)
      break
    case '1M':
      cutoff.setUTCMonth(cutoff.getUTCMonth() - 1)
      break
    case '3M':
      cutoff.setUTCMonth(cutoff.getUTCMonth() - 3)
      break
    case 'YTD':
      cutoff.setUTCMonth(0, 1)
      break
    case '1Y':
      cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1)
      break
  }
  const first = new Date(points[0].date);
  const effectiveCutoff = cutoff < first ? first : cutoff
  return points.filter((p) => new Date(p.date) >= effectiveCutoff)
}

// ---------------------------------------------------------------------- //
// Recommended trades (Dashboard, Research)
// ---------------------------------------------------------------------- //

export interface Recommendation {
  id: string
  symbol: string
  /** Strike or strike pair, pre-formatted for display: '$235', '$560/$555'. */
  strike: string
  /** Contract structure: 'Call', 'Put', 'Put Credit Spread'. */
  structure: string
  /** Calendar date (YYYY-MM-DD), not an instant — render with
   * format.ts#formatExpiry, which formats in UTC. Formatting this in ET
   * shifts it a day earlier. */
  expiry: string
  /** Setup class from the taxonomy. Drives the confidence base rate
   * (PRD.md §6.3) — not the same thing as the human-readable reason. */
  setup: string
  /** Why this surfaced: the scanner rule that matched, or the LLM's
   * annotation (PRD.md §6.1). */
  reason: string
  confidence: number | null // null renders as "—" — no base rate yet
  unvalidated: boolean
  origin: 'scanner' | 'llm'
}

/** `SPY $560/$555 Put Credit Spread` */
export function recommendationTitle(r: Recommendation): string {
  return `${r.symbol} ${r.strike} ${r.structure}`
}

/* Deliberately spans every confidence state the UI can render — high
 * (>=65), medium (50-64), low (<50), and null — so all four are visible
 * on screen rather than only the ones that happen to occur.
 *
 * Null splits two ways in the confidence slot, and both need a row: an
 * unvalidated LLM origination (rec-4) shows the tag, while a scanner
 * candidate whose setup class has no base rate yet (rec-7) shows the em
 * dash. Without rec-7 the em-dash branch is unreachable on screen. */
export const RECOMMENDATIONS: Recommendation[] = [
  { id: 'rec-1', symbol: 'SPY', strike: '$560/$555', structure: 'Put Credit Spread', expiry: '2026-11-21', setup: 'mean_reversion', reason: 'RSI 28, below 20d SMA', confidence: 71, unvalidated: false, origin: 'scanner' },
  { id: 'rec-2', symbol: 'AAPL', strike: '$235', structure: 'Call', expiry: '2026-12-19', setup: 'earnings_drift', reason: 'Beat by 6%, drift intact', confidence: 64, unvalidated: false, origin: 'scanner' },
  { id: 'rec-3', symbol: 'NVDA', strike: '$145/$140', structure: 'Put Credit Spread', expiry: '2026-11-21', setup: 'iv_crush', reason: 'IV rank 82, earnings passed', confidence: 58, unvalidated: false, origin: 'scanner' },
  { id: 'rec-4', symbol: 'QQQ', strike: '$495', structure: 'Call', expiry: '2027-01-16', setup: 'unclassified', reason: 'LLM: momentum + soft CPI', confidence: null, unvalidated: true, origin: 'llm' },
  { id: 'rec-5', symbol: 'TSLA', strike: '$260/$250', structure: 'Put Credit Spread', expiry: '2026-12-19', setup: 'mean_reversion', reason: 'RSI 31, held support at $250', confidence: 69, unvalidated: false, origin: 'scanner' },
  { id: 'rec-6', symbol: 'IWM', strike: '$205', structure: 'Put', expiry: '2026-12-19', setup: 'gap_fade', reason: 'Gapped 2.1% on no news', confidence: 44, unvalidated: false, origin: 'scanner' },
  { id: 'rec-7', symbol: 'AMD', strike: '$185/$180', structure: 'Put Credit Spread', expiry: '2027-01-16', setup: 'post_split_drift', reason: 'New setup class, 4 samples', confidence: null, unvalidated: false, origin: 'scanner' },
]

// ---------------------------------------------------------------------- //
// Executions / activity (Dashboard "Recent Executions", Activity page)
// ---------------------------------------------------------------------- //

export type ActivityStatus = 'filled' | 'rejected' | 'pending' | 'canceled'

export const ACTIVITY_STATUS_LABEL: Record<ActivityStatus, string> = {
  filled: 'Filled',
  rejected: 'Rejected',
  pending: 'Pending',
  canceled: 'Canceled',
}
export type ActivityAction = 'BTO' | 'STC' | 'STO' | 'BTC' | 'DEPOSIT' | 'WITHDRAWAL'

/** Order actions keep their standard options abbreviations — BTO/STC/STO/
 * BTC are the jargon, not shorthand to be expanded. Cash movements are
 * not orders and read as plain words. */
export const ACTIVITY_ACTION_LABEL: Record<ActivityAction, string> = {
  BTO: 'BTO',
  STC: 'STC',
  STO: 'STO',
  BTC: 'BTC',
  DEPOSIT: 'Deposit',
  WITHDRAWAL: 'Withdrawal',
}

/** A deposit is a cash movement, not an order — nothing was bought, sold,
 * filled or rejected. The Dashboard's Recent Executions is a trading feed
 * and shows orders only; Activity is the account's full ledger and shows
 * both (PRD.md §8.2). Keep the two readings apart here rather than
 * re-deriving the distinction at each call site. */
export function isOrderAction(action: ActivityAction): boolean {
  return action !== 'DEPOSIT' && action !== 'WITHDRAWAL'
}

/** Text color for a rendered activity status.
 *
 * Read by the `full` layout of ExecutionsTable — Activity's Status column.
 * The Dashboard's `summary` layout has no room for one and puts the status
 * on hover instead, so this map is live on one page of the two. Keeping it
 * here rather than beside either table is what lets the two agree.
 *
 * `rejected` takes `error`, not `bearish` — DESIGN.md assigns error to
 * "Rejections, failures", and a rejected order is a system/rule outcome
 * rather than a losing position. Both clear 6.1:1 on surface in light and
 * 10.9:1 in dark; verified. */
export const ACTIVITY_STATUS_CLASS: Record<ActivityStatus, string> = {
  filled: 'text-on-surface-variant',
  rejected: 'text-error',
  pending: 'text-caution',
  canceled: 'text-on-surface-variant',
}

export interface ActivityItem {
  id: string
  time: string // ISO datetime
  contract: string
  action: ActivityAction
  price: number | null
  quantity: number | null
  pnl: number | null
  /** Return on the closed trade as a percentage of cost basis. Null
   * wherever `pnl` is null — an opening fill has realized nothing yet, and
   * a rejection never will. Carried beside `pnl` rather than derived from
   * it: the dollar figure and the percentage answer different questions
   * (how much, how well), and the Activity header averages both
   * separately (PRD.md §8.2). */
  pnlPct: number | null
  /** Signed cash movement for DEPOSIT / WITHDRAWAL — positive in, negative
   * out. Null on trades, which report `pnl` instead. Kept separate from
   * `pnl` on purpose: money you moved into the account is not money the
   * account made, and summing the two would overstate performance. */
  amount: number | null
  status: ActivityStatus
  rejectionReason?: string
}

/** A rejection is logged with the rule that produced it, never a bare
 * "Rejected" (CLAUDE.md rule 8, PRD.md §4). These name the real limit keys
 * from PRD.md §4, so the string read on Activity points at the same
 * setting that is editable in Settings. */
const REJECTION_REASONS = [
  'max_risk_per_trade_pct: sized at 9.2% of equity (limit 7%)',
  'max_concurrent_positions: 8 already open (limit 8)',
  'max_net_directional_pct: net long 44% of equity (limit 40%)',
  'max_daily_loss_pct: down 21% of starting-day equity (limit 20%) — halted',
]

/* The hand-written head of each feed exists for *coverage*: between them
 * these rows hit all four statuses, a deposit, a withdrawal, a rejection
 * carrying its reason, an opening fill with no P&L yet, and a close that
 * lost money. The generated tail below adds pages, not states — so don't
 * thin these out, and don't rely on the tail to reach a state.
 *
 * `pending` in particular was unreachable before the Activity page existed:
 * ACTIVITY_STATUS_CLASS has always had a `caution` branch for it that no
 * fixture ever rendered. */
const PAPER_ACTIVITY_HEAD: ActivityItem[] = [
  // Pending, and the counterpart of WORKING_ORDERS' wo-1 below: a limit
  // sell that hasn't filled. The two are linked by id so cancelling the
  // order flips this row rather than appending a second one.
  { id: 'act-1', time: '2026-08-07T15:58:00Z', contract: 'TSLA $240 Put Nov 15', action: 'STC', price: 3.4, quantity: 1, pnl: null, pnlPct: null, amount: null, status: 'pending' },
  { id: 'act-2', time: '2026-08-07T14:32:00Z', contract: 'AAPL $230 Call Aug 15', action: 'STC', price: 4.85, quantity: 2, pnl: 62.0, pnlPct: 17.71, amount: null, status: 'filled' },
  { id: 'act-3', time: '2026-08-07T13:05:00Z', contract: 'TSLA $240 Put Nov 15', action: 'BTO', price: 4.1, quantity: 1, pnl: null, pnlPct: null, amount: null, status: 'filled' },
  { id: 'act-4', time: '2026-08-07T10:48:00Z', contract: 'SPY $430/$425 Put Credit Spread Oct 17', action: 'STO', price: 0.7, quantity: 3, pnl: null, pnlPct: null, amount: null, status: 'filled' },
  { id: 'act-5', time: '2026-08-06T19:58:00Z', contract: 'QQQ $370 Call Dec 20', action: 'BTO', price: 6.4, quantity: 1, pnl: null, pnlPct: null, amount: null, status: 'rejected', rejectionReason: 'max_exposure_per_underlying: QQQ already at 27% of equity (limit 25%)' },
  { id: 'act-6', time: '2026-08-06T15:41:00Z', contract: 'NVDA $150 Put Nov 21', action: 'STC', price: 2.15, quantity: 2, pnl: -102.5, pnlPct: -25.0, amount: null, status: 'filled' },
  { id: 'act-7', time: '2026-08-06T09:31:00Z', contract: '—', action: 'DEPOSIT', price: null, quantity: null, pnl: null, pnlPct: null, amount: 5_000.0, status: 'filled' },
  { id: 'act-8', time: '2026-08-05T16:02:00Z', contract: 'AMZN $185 Call Sep 19', action: 'BTO', price: 3.2, quantity: 4, pnl: null, pnlPct: null, amount: null, status: 'canceled' },
  { id: 'act-9', time: '2026-08-04T18:12:00Z', contract: '—', action: 'WITHDRAWAL', price: null, quantity: null, pnl: null, pnlPct: null, amount: -1_250.0, status: 'filled' },
]

const CASH_ACTIVITY_HEAD: ActivityItem[] = [
  { id: 'cash-act-1', time: '2026-08-07T15:12:00Z', contract: 'MSFT $410/$400 Put Credit Spread Oct 17', action: 'STO', price: 0.7, quantity: 2, pnl: null, pnlPct: null, amount: null, status: 'filled' },
  { id: 'cash-act-2', time: '2026-08-06T17:24:00Z', contract: 'SPY $425 Put Sep 19', action: 'BTO', price: 1.86, quantity: 1, pnl: null, pnlPct: null, amount: null, status: 'filled' },
  { id: 'cash-act-3', time: '2026-08-06T13:47:00Z', contract: 'NVDA $140 Call Oct 17', action: 'BTO', price: 5.2, quantity: 2, pnl: null, pnlPct: null, amount: null, status: 'rejected', rejectionReason: 'max_risk_per_trade_pct: $1,040 is 10.7% of equity (limit 7%)' },
  { id: 'cash-act-4', time: '2026-08-05T13:31:00Z', contract: '—', action: 'DEPOSIT', price: null, quantity: null, pnl: null, pnlPct: null, amount: 2_000.0, status: 'filled' },
]

/** Contract strings for the generated tail. Real-looking underlyings and
 * structures, because a scanned column of them should read as a trading
 * log rather than as filler. */
const TAIL_CONTRACTS = [
  'AAPL $225 Call Sep 19',
  'AAPL $235/$240 Call Debit Spread Oct 17',
  'MSFT $410 Call Oct 17',
  'NVDA $145/$140 Put Credit Spread Nov 21',
  'SPY $560/$555 Put Credit Spread Dec 19',
  'QQQ $495 Call Jan 16',
  'TSLA $260 Put Sep 19',
  'AMD $185 Call Nov 21',
  'IWM $205 Put Oct 17',
  'AMZN $190/$185 Put Credit Spread Sep 19',
  'GOOGL $175 Call Dec 19',
  'META $520 Put Nov 21',
]

/** Walks back one trading session. Same market-closed rule the price
 * series uses — a feed with Saturday fills in it is a feed nobody trusts. */
function previousSession(day: Date): Date {
  const d = new Date(day)
  do {
    d.setUTCDate(d.getUTCDate() - 1)
  } while (d.getUTCDay() === 0 || d.getUTCDay() === 6)
  return d
}

const round2 = (n: number) => Math.round(n * 100) / 100

/** Depth for the Activity page's pagination. The head above already covers
 * every state the table can render, so this only has to look like more of
 * the same. Runs on its own seed rather than the module-level stream, so
 * adding it left every series above byte-identical — same reason
 * CASH_HISTORY has its own seed.
 *
 * Only a *filled closing* trade carries P&L: an opening fill has realized
 * nothing, and a rejected or canceled order never will.
 *
 * `maxQuantity` and `priceCeiling` are set per account so that no generated
 * order risks more than max_risk_per_trade_pct (7%) of that account's
 * equity. A fixture that violates the project's own risk limits is a
 * fixture that teaches the wrong thing about what the engine allows. */
function buildActivityTail(
  seed: number,
  count: number,
  idPrefix: string,
  startFrom: string,
  maxQuantity: number,
  priceCeiling: number,
): ActivityItem[] {
  const next = mulberry32(seed)
  const items: ActivityItem[] = []
  let day = new Date(`${startFrom}T00:00:00Z`)
  let remainingToday = 0

  while (items.length < count) {
    if (remainingToday === 0) {
      day = previousSession(day)
      remainingToday = 1 + Math.floor(next() * 3)
    }
    remainingToday--

    // Inside the regular session: 09:30–16:00 ET is 13:30–20:00 UTC.
    const hour = 13 + Math.floor(next() * 7)
    const minute = Math.floor(next() * 60)
    const pad = (n: number) => String(n).padStart(2, '0')
    const time = `${day.toISOString().slice(0, 10)}T${pad(hour)}:${pad(minute)}:00Z`
    const id = `${idPrefix}-${items.length + 1}`

    // A cash movement now and then, so the feed isn't uniformly trades.
    if (next() < 0.05) {
      const deposit = next() < 0.6
      items.push({
        id,
        time,
        contract: '—',
        action: deposit ? 'DEPOSIT' : 'WITHDRAWAL',
        price: null,
        quantity: null,
        pnl: null,
        pnlPct: null,
        amount: round2((deposit ? 1 : -1) * (250 + next() * 2_250)),
        status: 'filled',
      })
      continue
    }

    const closing = next() < 0.5
    const short = next() < 0.45
    const action: ActivityAction = closing ? (short ? 'BTC' : 'STC') : short ? 'STO' : 'BTO'
    const quantity = 1 + Math.floor(next() * maxQuantity)
    const price = round2(0.4 + next() * priceCeiling)

    const statusRoll = next()
    const status: ActivityStatus =
      statusRoll < 0.85 ? 'filled' : statusRoll < 0.92 ? 'rejected' : statusRoll < 0.97 ? 'canceled' : 'pending'

    // Winners more often, and by a little more, than losers. These numbers
    // are not arbitrary: the feed has to agree with what the rest of the
    // app claims about this account. STRATEGIES.live puts strat-1 at a 71%
    // win rate and a 1.5 profit factor, so a break-even Activity page with
    // a 50% hit rate would have the two screens contradicting each other.
    const realized = status === 'filled' && closing
    const pnlPct = realized ? round2(next() < 0.64 ? 8 + next() * 46 : -(5 + next() * 32)) : null
    const pnl = pnlPct === null ? null : round2((price * quantity * 100 * pnlPct) / 100)

    items.push({
      id,
      time,
      contract: TAIL_CONTRACTS[Math.floor(next() * TAIL_CONTRACTS.length)],
      action,
      price,
      quantity,
      pnl,
      pnlPct,
      amount: null,
      status,
      rejectionReason:
        status === 'rejected' ? REJECTION_REASONS[Math.floor(next() * REJECTION_REASONS.length)] : undefined,
    })
  }

  return items
}

/** Paper trades more, and larger, than cash — same asymmetry the portfolio
 * series carries. Cash is real money and is sized accordingly. */
const PAPER_ACTIVITY: ActivityItem[] = [
  ...PAPER_ACTIVITY_HEAD,
  ...buildActivityTail(20260809, 44, 'act-p', '2026-08-04', 4, 4.5),
]

const CASH_ACTIVITY: ActivityItem[] = [
  ...CASH_ACTIVITY_HEAD,
  ...buildActivityTail(20260810, 20, 'cash-act-t', '2026-08-05', 2, 2.8),
]

export interface ActivityStats {
  /** Null where the account has no trade of that kind yet — an average
   * over zero trades is not zero, it is unknown, and rendering it as
   * $0.00 would claim a result that does not exist. */
  avgWin: number | null
  avgWinPct: number | null
  avgLoss: number | null
  avgLossPct: number | null
  lifetimePnl: number
  wins: number
  losses: number
}

/** The Activity header stats (PRD.md §8.2), computed from the feed rather
 * than stored as separate numbers — a header that disagrees with the rows
 * underneath it is worse than no header at all.
 *
 * Only rows with a realized P&L count. An opening fill, a rejection, and a
 * cash movement each have nothing to contribute to an average return.
 * `amount` is excluded from lifetime P&L for the same reason it is kept off
 * `pnl`: money you moved into the account is not money the account made. */
export function activityStats(items: ActivityItem[]): ActivityStats {
  const realized = items.flatMap((a) =>
    a.pnl !== null && a.pnlPct !== null ? [{ pnl: a.pnl, pnlPct: a.pnlPct }] : [],
  )
  const wins = realized.filter((r) => r.pnl > 0)
  const losses = realized.filter((r) => r.pnl < 0)
  const mean = (xs: number[]) => (xs.length === 0 ? null : xs.reduce((t, x) => t + x, 0) / xs.length)

  return {
    avgWin: mean(wins.map((r) => r.pnl)),
    avgWinPct: mean(wins.map((r) => r.pnlPct)),
    avgLoss: mean(losses.map((r) => r.pnl)),
    avgLossPct: mean(losses.map((r) => r.pnlPct)),
    lifetimePnl: round2(realized.reduce((t, r) => t + r.pnl, 0)),
    wins: wins.length,
    losses: losses.length,
  }
}

// ---------------------------------------------------------------------- //
// Open positions (Activity page)
// ---------------------------------------------------------------------- //

/** The standard options multiplier. One contract deliverable is 100
 * shares — except on an adjusted contract (`AAPL1`, issued after a split
 * or special dividend), where it is not, and every calculation here is
 * wrong. Phase 1 carries no adjusted contracts; Phase 2 must read the
 * multiplier off the contract rather than from this constant. */
export const CONTRACT_MULTIPLIER = 100

export type OrderType = 'market' | 'limit' | 'stop' | 'stop_limit'

/** The four order sides, in the jargon. Defined here rather than in
 * orders.ts because `WorkingOrder` below needs it and orders.ts already
 * imports this file — putting it there would make the two circular. */
export type OrderSide = 'BTO' | 'STC' | 'STO' | 'BTC'

export const ORDER_TYPE_LABEL: Record<OrderType, string> = {
  market: 'Market',
  limit: 'Limit',
  stop: 'Stop',
  stop_limit: 'Stop-Limit',
}

/** Day and GTC are the only two an option order accepts — IOC, FOK, OPG
 * and CLS are all rejected. Verified against Alpaca's order-type matrix. */
export type TimeInForce = 'day' | 'gtc'

export const TIME_IN_FORCE_LABEL: Record<TimeInForce, string> = {
  day: 'Day',
  gtc: 'GTC',
}

/** Where an attached exit physically lives. The difference is whether it
 * survives Corollary being down: a broker-side OCO fires regardless, a
 * Corollary-managed exit does not exist while the engine is stopped. The
 * row says which, because that is not a detail. */
export type ExitHolder = 'broker' | 'corollary'

export interface PositionLeg {
  /** OCC format: underlying + YYMMDD + C/P + 8-digit strike ×1000. */
  symbol: string
  strike: number
  right: 'call' | 'put'
  side: 'long' | 'short'
  /** Alpaca requires leg ratios in simplest form — the GCD across a
   * multi-leg order's ratios must be 1, or the order is rejected. */
  ratio: number
}

/** A manual exit attached to a position, modelled on Alpaca's
 * `order_class: oco`: a take-profit limit paired with a stop, where
 * supplying `stopLimitPrice` makes the stop leg a stop-limit.
 *
 * A position holds at most one of these. Editing replaces it in place —
 * cancelling and re-submitting would leave a window with no exit on the
 * position at all, which is the window a fast market runs through. */
export interface AttachedExit {
  takeProfit: number
  stopPrice: number
  stopLimitPrice: number | null
  timeInForce: TimeInForce
  heldBy: ExitHolder
}

/** The exits a strategy manages on its own positions (PRD.md §5.1). These
 * stop applying the moment the position is detached. */
export interface ManagedExit {
  profitTargetPct: number
  stopLossPct: number
  timeStopDte: number
}

export interface Position {
  id: string
  symbol: string
  contract: string
  /** The **contract's** last traded price, not the underlying's — it sits
   * between `bid` and `ask`, which is what anyone reading a row of three
   * price columns already assumes. It held the underlying's price until
   * the Open Positions rework; one row carrying two instruments with
   * nothing in the names to say so was a bug waiting for a chart. */
  last: number
  /** The underlying's price. Read by the payoff curve and nothing else. */
  underlying: number
  costBasis: number
  value: number
  quantity: number
  pnl: number
  pnlPct: number
  bid: number
  ask: number
  /** Which side the position is on. Determines how it closes — a long is
   * sold to close at the bid, a short is bought to close at the ask — so
   * Flatten cannot generate a correct execution without it. */
  direction: 'long' | 'short'
  /** More than one leg means multi-leg, which Alpaca will only accept as a
   * limit order. Order-type availability is derived from this, never
   * hardcoded per position. */
  legs: PositionLeg[]
  /** Calendar date (YYYY-MM-DD), not an instant — the single most
   * time-sensitive fact about an option, and until now it existed only
   * inside the contract string and the legs' OCC symbols, where nothing
   * could count it down. Render with `formatExpiry`, which formats in UTC;
   * formatting a date-only value in ET shows the previous day. */
  expiry: string
  /** Which strategy manages this position, or null once detached. */
  strategyId: string | null
  /** Which strategy opened it. Never cleared, so detaching is reversible:
   * without this, reattaching could only guess, and the obvious guess —
   * whichever strategy happens to be active now — is wrong whenever you've
   * switched strategies since the position was opened. */
  openedByStrategyId: string
  managedExit: ManagedExit | null
  attachedExit: AttachedExit | null
  /** Position value over the life of the position. Starts at `costBasis`
   * and ends at `value` — see buildValueHistory. */
  valueHistory: PricePoint[]
}

/** Position value from entry to now. The endpoints are pinned rather than
 * generated: the series has to start at the position's cost basis and end
 * at its current value, or the chart quietly contradicts the row it
 * expands from, which is worse than drawing no chart at all. The random
 * walk only shapes the path between two fixed points. */
function buildValueHistory(seed: number, costBasis: number, value: number, days: number): PricePoint[] {
  const next = mulberry32(seed)
  const points: PricePoint[] = []
  const start = new Date('2026-08-07T00:00:00Z')
  start.setUTCDate(start.getUTCDate() - days)

  let day = new Date(start)
  const sessions: Date[] = []
  while (sessions.length < days) {
    if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) sessions.push(new Date(day))
    day.setUTCDate(day.getUTCDate() + 1)
  }

  const span = value - costBasis
  sessions.forEach((d, i) => {
    const t = i / (sessions.length - 1)
    // Drift from cost basis to current value, wobbling around the line.
    // Amplitude tapers to zero at both ends so the pinned endpoints don't
    // arrive as a visible discontinuity.
    const wobble = (next() - 0.5) * costBasis * 0.18 * Math.sin(Math.PI * t)
    const raw = costBasis + span * t + wobble
    points.push({ date: d.toISOString().slice(0, 10), value: Math.round(Math.max(raw, 1) * 100) / 100 })
  })

  points[0] = { ...points[0], value: costBasis }
  points[points.length - 1] = { ...points[points.length - 1], value }
  return points
}

/* Both books carry a long and a short, because the two close along
 * different paths — a long is sold to close at the bid, a short is bought
 * to close at the ask — and Close is a per-row action on Activity in both
 * accounts. A book with only longs leaves the BTC path unexercised. Each
 * book also carries a multi-leg position, so the limit-only order path is
 * reachable in either account.
 *
 * Every position here is internally consistent, and `mockData.test.ts`
 * asserts it: `last` sits within [bid, ask], `value` equals
 * last × quantity × 100, and `pnl` runs the right way for the direction —
 * a long gains as value rises, a short as it falls. */
const PAPER_POSITIONS: Position[] = [
  {
    id: 'pos-1', symbol: 'AAPL', contract: '$230 Call Oct 17',
    last: 2.06, underlying: 232.4, costBasis: 350.0, value: 412.0, quantity: 2,
    pnl: 62.0, pnlPct: 17.71, bid: 2.04, ask: 2.08, direction: 'long',
    legs: [{ symbol: 'AAPL261017C00230000', strike: 230, right: 'call', side: 'long', ratio: 1 }],
    expiry: '2026-10-17',
    strategyId: 'strat-1',
    openedByStrategyId: 'strat-1',
    managedExit: { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 },
    attachedExit: null,
    valueHistory: buildValueHistory(20260901, 350.0, 412.0, 24),
  },
  {
    id: 'pos-2', symbol: 'TSLA', contract: '$240 Put Nov 15',
    last: 3.0, underlying: 238.1, costBasis: 400.0, value: 300.0, quantity: 1,
    pnl: -100.0, pnlPct: -25.0, bid: 2.96, ask: 3.04, direction: 'long',
    legs: [{ symbol: 'TSLA261115P00240000', strike: 240, right: 'put', side: 'long', ratio: 1 }],
    expiry: '2026-11-15',
    strategyId: 'strat-1',
    openedByStrategyId: 'strat-1',
    managedExit: { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 },
    attachedExit: null,
    valueHistory: buildValueHistory(20260902, 400.0, 300.0, 18),
  },
  {
    id: 'pos-3', symbol: 'SPY', contract: '$430/$425 Put Credit Spread Oct 17',
    last: 0.56, underlying: 429.88, costBasis: 210.0, value: 168.0, quantity: 3,
    pnl: 42.0, pnlPct: 20.0, bid: 0.55, ask: 0.6, direction: 'short',
    legs: [
      { symbol: 'SPY261017P00430000', strike: 430, right: 'put', side: 'short', ratio: 1 },
      { symbol: 'SPY261017P00425000', strike: 425, right: 'put', side: 'long', ratio: 1 },
    ],
    expiry: '2026-10-17',
    strategyId: 'strat-1',
    openedByStrategyId: 'strat-1',
    managedExit: { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 },
    attachedExit: null,
    valueHistory: buildValueHistory(20260903, 210.0, 168.0, 21),
  },
  // Flat, deliberately: signClass has a zero branch and text-on-surface-variant
  // is the one colour a P&L column reaches for that isn't a gain or a loss.
  // Also the one position already carrying a manual exit, so the "Edit exit"
  // state and the broker/Corollary label are both reachable on load.
  {
    id: 'pos-4', symbol: 'QQQ', contract: '$370 Call Aug 14',
    last: 6.4, underlying: 372.4, costBasis: 640.0, value: 640.0, quantity: 1,
    pnl: 0, pnlPct: 0, bid: 6.35, ask: 6.45, direction: 'long',
    legs: [{ symbol: 'QQQ260814C00370000', strike: 370, right: 'call', side: 'long', ratio: 1 }],
    expiry: '2026-08-14',
    strategyId: null,
    openedByStrategyId: 'strat-1',
    managedExit: null,
    attachedExit: { takeProfit: 9.6, stopPrice: 4.5, stopLimitPrice: 4.4, timeInForce: 'gtc', heldBy: 'broker' },
    valueHistory: buildValueHistory(20260904, 640.0, 640.0, 15),
  },
  /* The last two exist so the DTE column's other render paths are
   * reachable. Without them `Expired` and `Today` are branches nobody can
   * ever see, which is the same trap `pending` fell into before the
   * Activity page existed.
   *
   * An expired contract sitting in the book is not a contrivance: it stays
   * there until settlement clears it, and those are exactly the hours when
   * you most want the row shouting about it. Both are near-worthless
   * out-of-the-money longs, which is how most options end. */
  {
    id: 'pos-5', symbol: 'AAPL', contract: '$240 Call Aug 7',
    last: 0.05, underlying: 232.4, costBasis: 120.0, value: 5.0, quantity: 1,
    pnl: -115.0, pnlPct: -95.83, bid: 0.03, ask: 0.07, direction: 'long',
    legs: [{ symbol: 'AAPL260807C00240000', strike: 240, right: 'call', side: 'long', ratio: 1 }],
    expiry: '2026-08-07',
    strategyId: 'strat-1',
    openedByStrategyId: 'strat-1',
    managedExit: { profitTargetPct: 50, stopLossPct: 200, timeStopDte: 2 },
    attachedExit: null,
    valueHistory: buildValueHistory(20260907, 120.0, 5.0, 9),
  },
  {
    id: 'pos-6', symbol: 'SPY', contract: '$420 Put Aug 5',
    last: 0.02, underlying: 429.88, costBasis: 90.0, value: 2.0, quantity: 1,
    pnl: -88.0, pnlPct: -97.78, bid: 0.01, ask: 0.03, direction: 'long',
    legs: [{ symbol: 'SPY260805P00420000', strike: 420, right: 'put', side: 'long', ratio: 1 }],
    expiry: '2026-08-05',
    strategyId: null,
    openedByStrategyId: 'strat-1',
    managedExit: null,
    attachedExit: null,
    valueHistory: buildValueHistory(20260908, 90.0, 2.0, 11),
  },
]

const CASH_POSITIONS: Position[] = [
  {
    id: 'cash-pos-1', symbol: 'SPY', contract: '$425 Put Sep 19',
    last: 1.62, underlying: 429.88, costBasis: 186.0, value: 162.0, quantity: 1,
    pnl: -24.0, pnlPct: -12.9, bid: 1.6, ask: 1.66, direction: 'long',
    legs: [{ symbol: 'SPY260919P00425000', strike: 425, right: 'put', side: 'long', ratio: 1 }],
    expiry: '2026-09-19',
    strategyId: 'strat-2',
    openedByStrategyId: 'strat-2',
    managedExit: { profitTargetPct: 40, stopLossPct: 150, timeStopDte: 3 },
    attachedExit: null,
    valueHistory: buildValueHistory(20260905, 186.0, 162.0, 12),
  },
  {
    id: 'cash-pos-2', symbol: 'MSFT', contract: '$410/$400 Put Credit Spread Oct 17',
    last: 0.49, underlying: 418.35, costBasis: 140.0, value: 98.0, quantity: 2,
    pnl: 42.0, pnlPct: 30.0, bid: 0.47, ask: 0.52, direction: 'short',
    legs: [
      { symbol: 'MSFT261017P00410000', strike: 410, right: 'put', side: 'short', ratio: 1 },
      { symbol: 'MSFT261017P00400000', strike: 400, right: 'put', side: 'long', ratio: 1 },
    ],
    // Detached and Corollary-managed, so the counterpart to pos-4's
    // broker-held exit is on screen somewhere too.
    expiry: '2026-10-17',
    strategyId: null,
    openedByStrategyId: 'strat-2',
    managedExit: null,
    attachedExit: { takeProfit: 0.2, stopPrice: 1.1, stopLimitPrice: null, timeInForce: 'day', heldBy: 'corollary' },
    valueHistory: buildValueHistory(20260906, 140.0, 98.0, 16),
  },
]

// ---------------------------------------------------------------------- //
// Underlyings (Activity page — the underlying behind an option)
// ---------------------------------------------------------------------- //

export interface UnderlyingQuote {
  symbol: string
  price: number
  /** Yesterday's close. The daily change is measured from here, not from
   * the first point of the series — a 60-session chart's left edge is two
   * months ago and "today" measured against it is not today. */
  previousClose: number
  change: number
  changePct: number
  history: PricePoint[]
}

/** Keyed by symbol, not carried on the position, because two positions can
 * share an underlying — SPY backs both a paper spread and a cash put here.
 * Storing a copy per position would let the same stock show two different
 * prices on two rows of the same page. */
function buildUnderlying(symbol: string, price: number, seed: number, sessions: number): UnderlyingQuote {
  const next = mulberry32(seed)
  const points: PricePoint[] = []

  // Walk backwards from today's price so the series *ends* where the
  // position says the underlying is, then reverse. Generating forwards and
  // hoping to land on the right number would put the chart and the row in
  // disagreement.
  let value = price
  const day = new Date('2026-08-07T00:00:00Z')
  for (let i = 0; i < sessions; i++) {
    if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) {
      points.push({ date: day.toISOString().slice(0, 10), value: Math.round(value * 100) / 100 })
      value = value * (1 - (next() - 0.5) * 0.022)
    }
    day.setUTCDate(day.getUTCDate() - 1)
  }
  points.reverse()

  const previousClose = points[points.length - 2].value
  const change = Math.round((price - previousClose) * 100) / 100
  return {
    symbol,
    price,
    previousClose,
    change,
    changePct: Math.round((change / previousClose) * 10_000) / 100,
    history: points,
  }
}

/** Prices here must match every `Position.underlying` that names the same
 * symbol; `orders.test.ts` asserts it. */
export const UNDERLYINGS: Record<string, UnderlyingQuote> = {
  AAPL: buildUnderlying('AAPL', 232.4, 20261001, 62),
  TSLA: buildUnderlying('TSLA', 238.1, 20261002, 62),
  SPY: buildUnderlying('SPY', 429.88, 20261003, 62),
  QQQ: buildUnderlying('QQQ', 372.4, 20261004, 62),
  MSFT: buildUnderlying('MSFT', 418.35, 20261005, 62),
  NVDA: buildUnderlying('NVDA', 138.2, 20261006, 62),
}

// ---------------------------------------------------------------------- //
// Working orders (Activity page)
// ---------------------------------------------------------------------- //

/** An order that has been placed and hasn't filled.
 *
 * Only non-market orders appear here: a market order fills, it does not
 * sit and work. Until this existed the terminal had no concept of "an
 * order I placed that hasn't happened yet", which made Activity a record
 * of the past rather than the ledger of record it claims to be.
 *
 * Attached exits are deliberately *not* modelled here. They live on the
 * position, which is where they are edited and cancelled, and duplicating
 * them into a second list would give the same thing two homes that could
 * disagree. */
export interface WorkingOrder {
  id: string
  positionId: string
  /** Denormalised for display, so the list renders without resolving the
   * position — which may have been closed out from under it. */
  contract: string
  side: OrderSide
  orderType: Exclude<OrderType, 'market'>
  quantity: number
  limitPrice: number | null
  stopPrice: number | null
  timeInForce: TimeInForce
  placedAt: string
  /** The pending row this order wrote to the activity feed. Cancelling
   * flips that row to `canceled` rather than appending a second one — the
   * order had one life and the ledger should show it once. */
  activityId: string
}

/** Paper carries one so the populated state is on screen; cash carries
 * none so the empty state is too. Both are reachable by toggling the
 * account rather than by contriving a sequence of clicks. */
const PAPER_WORKING_ORDERS: WorkingOrder[] = [
  {
    id: 'wo-1',
    positionId: 'pos-2',
    contract: 'TSLA $240 Put Nov 15',
    side: 'STC',
    orderType: 'limit',
    quantity: 1,
    limitPrice: 3.4,
    stopPrice: null,
    timeInForce: 'gtc',
    placedAt: '2026-08-07T15:58:00Z',
    activityId: 'act-1',
  },
]

const CASH_WORKING_ORDERS: WorkingOrder[] = []

// ---------------------------------------------------------------------- //
// News (News page)
// ---------------------------------------------------------------------- //

export type Sentiment = 'bullish' | 'bearish' | 'neutral' | 'unclassified'

export interface NewsItem {
  id: string
  time: string
  ticker: string // or "MARKET"
  headline: string
  sentiment: Sentiment
  publisher: string
  sector: string
}

export const NEWS_ITEMS: NewsItem[] = [
  { id: 'news-1', time: '2026-08-07T13:10:00Z', ticker: 'AAPL', headline: 'Apple beats Q3 estimates on services growth', sentiment: 'bullish', publisher: 'Alpaca News', sector: 'Technology' },
  { id: 'news-2', time: '2026-08-07T12:40:00Z', ticker: 'MARKET', headline: 'Fed holds rates, signals two cuts possible in 2027', sentiment: 'neutral', publisher: 'Finnhub', sector: 'Macro' },
  { id: 'news-3', time: '2026-08-07T11:55:00Z', ticker: 'TSLA', headline: 'Tesla recalls 120,000 vehicles over software issue', sentiment: 'bearish', publisher: 'Finnhub', sector: 'Consumer Discretionary' },
  { id: 'news-4', time: '2026-08-07T10:20:00Z', ticker: 'NVDA', headline: 'Analyst raises NVDA price target ahead of earnings', sentiment: 'bullish', publisher: 'Alpaca News', sector: 'Technology' },
  { id: 'news-5', time: '2026-08-07T09:05:00Z', ticker: 'XOM', headline: 'Exxon announces secondary offering', sentiment: 'unclassified', publisher: 'Finnhub', sector: 'Energy' },
]

// ---------------------------------------------------------------------- //
// Market sentiment composite (News page)
// ---------------------------------------------------------------------- //

export interface SentimentComponent {
  name: string
  description: string
  score: number // 0-100, z-scored then rescaled
}

export const SENTIMENT_COMPOSITE = 58
export const SENTIMENT_COMPONENTS: SentimentComponent[] = [
  { name: 'Momentum', description: 'SPX vs 125-day moving average', score: 64 },
  { name: 'Strength', description: '52-week highs vs lows', score: 55 },
  { name: 'Breadth', description: 'Advance/decline volume', score: 51 },
  { name: 'Put/call ratio', description: 'CBOE equity put/call', score: 60 },
  { name: 'Volatility', description: 'VIX vs 50-day moving average', score: 62 },
  { name: 'Safe-haven demand', description: '20-day equity minus Treasury return', score: 57 },
  { name: 'Junk bond demand', description: 'FRED BAMLH0A0HYM2', score: 57 },
]

// ---------------------------------------------------------------------- //
// Social attention (News page)
// ---------------------------------------------------------------------- //

export interface SocialAttentionItem {
  ticker: string
  mentions: number
  baselineMentions: number
  labeledCount: number
  sampleSize: number
  sentiment: Sentiment
}

export const SOCIAL_ATTENTION: SocialAttentionItem[] = [
  { ticker: 'NVDA', mentions: 4820, baselineMentions: 2100, labeledCount: 1740, sampleSize: 4820, sentiment: 'bullish' },
  { ticker: 'TSLA', mentions: 3910, baselineMentions: 3400, labeledCount: 1390, sampleSize: 3910, sentiment: 'bearish' },
  { ticker: 'AAPL', mentions: 2240, baselineMentions: 2000, labeledCount: 820, sampleSize: 2240, sentiment: 'bullish' },
  { ticker: 'SPY', mentions: 1650, baselineMentions: 1700, labeledCount: 540, sampleSize: 1650, sentiment: 'neutral' },
]

// ---------------------------------------------------------------------- //
// Sector consensus (News page — "Top rated by sector")
// ---------------------------------------------------------------------- //

export interface SectorConsensus {
  sector: string
  etf: string
  leader: string
  buy: number
  hold: number
  sell: number
  asOf: string
}

export const SECTOR_CONSENSUS: SectorConsensus[] = [
  { sector: 'Technology', etf: 'XLK', leader: 'AAPL', buy: 68, hold: 27, sell: 5, asOf: '2026-08-01' },
  { sector: 'Health Care', etf: 'XLV', leader: 'LLY', buy: 61, hold: 32, sell: 7, asOf: '2026-08-01' },
  { sector: 'Financials', etf: 'XLF', leader: 'BRK.B', buy: 55, hold: 38, sell: 7, asOf: '2026-08-01' },
  { sector: 'Consumer Discretionary', etf: 'XLY', leader: 'AMZN', buy: 64, hold: 29, sell: 7, asOf: '2026-08-01' },
  { sector: 'Energy', etf: 'XLE', leader: 'XOM', buy: 47, hold: 41, sell: 12, asOf: '2026-08-01' },
]

// ---------------------------------------------------------------------- //
// Market calendar (News page)
// ---------------------------------------------------------------------- //

export type CalendarEventType = 'earnings' | 'economic' | 'central-bank' | 'dividend' | 'geopolitical'

export interface CalendarEvent {
  id: string
  time: string
  type: CalendarEventType
  title: string
  ticker?: string
}

export const CALENDAR_EVENTS: CalendarEvent[] = [
  { id: 'cal-1', time: '2026-08-08T12:30:00Z', type: 'economic', title: 'Nonfarm payrolls' },
  { id: 'cal-2', time: '2026-08-08T20:05:00Z', type: 'earnings', title: 'Q3 earnings call', ticker: 'DIS' },
  { id: 'cal-3', time: '2026-08-11T18:00:00Z', type: 'central-bank', title: 'FOMC rate decision' },
  { id: 'cal-4', time: '2026-08-12T00:00:00Z', type: 'dividend', title: 'Ex-dividend date', ticker: 'JNJ' },
  { id: 'cal-5', time: '2026-08-13T00:00:00Z', type: 'geopolitical', title: 'EU trade council session' },
]

// ---------------------------------------------------------------------- //
// Option chains + stocks/ETFs (Markets page)
// ---------------------------------------------------------------------- //

export interface OptionContract {
  symbol: string
  strike: number
  /** YYYY-MM-DD. A calendar date, not an instant — render it with
   * formatExpiry, which parses UTC. Formatting it in ET shows the
   * previous day. */
  expiration: string
  type: 'call' | 'put'
  last: number
  change: number
  changePct: number
  bid: number
  ask: number
  volume: number
  openInterest: number
  iv: number
}

/** The three expirations the chain is built over — all Fridays, which is
 * when standard US equity options actually expire. Forward-dated from
 * MARKET_TODAY at 14 / 42 / 70 days so the near, middle and far ends of
 * the curve are all on screen and time value visibly decays across them. */
export const CHAIN_EXPIRATIONS = ['2026-08-21', '2026-09-18', '2026-10-16']

/** Underlyings with a listed chain.
 *
 * Spot is deliberately *not* repeated here — it is read from UNDERLYINGS,
 * so the Markets chain and the Activity position rows cannot disagree
 * about what AAPL costs. `liquidity` scales volume and open interest: SPY
 * trades orders of magnitude more contracts than MSFT, and a chain where
 * every name is equally busy makes "most volume" meaningless. */
const CHAIN_UNDERLYINGS: { symbol: string; baseIv: number; liquidity: number; seed: number }[] = [
  { symbol: 'SPY', baseIv: 0.16, liquidity: 3.2, seed: 20262001 },
  { symbol: 'AAPL', baseIv: 0.28, liquidity: 1.4, seed: 20262002 },
  { symbol: 'NVDA', baseIv: 0.44, liquidity: 1.6, seed: 20262003 },
  { symbol: 'TSLA', baseIv: 0.52, liquidity: 1.1, seed: 20262004 },
  { symbol: 'QQQ', baseIv: 0.19, liquidity: 0.9, seed: 20262005 },
  { symbol: 'MSFT', baseIv: 0.24, liquidity: 0.35, seed: 20262006 },
]

/** Strike increments follow the listed ladder rather than a fixed step — a
 * $5 ladder on a $138 stock and a $10 ladder on a $430 index is what OPRA
 * actually lists, and evenly-spaced strikes across every price would make
 * the chain look generated. */
function strikeIncrement(spot: number): number {
  if (spot < 50) return 1
  if (spot < 100) return 2.5
  if (spot < 250) return 5
  return 10
}

/** Approximate call delta from standardised moneyness. Not Black-Scholes —
 * this is fixture data, and a logistic in sigma-units is close enough to
 * give the chain a coherent shape: deep ITM near 1, ATM near 0.5, far OTM
 * near 0. It exists so a day's change can be derived from the underlying's
 * move rather than drawn independently, which is what makes "top gainers"
 * a coherent list instead of noise. */
function callDelta(moneyness: number): number {
  return 1 / (1 + Math.exp(moneyness * 1.55))
}

function buildChain(): OptionContract[] {
  const contracts: OptionContract[] = []
  const today = new Date(`${MARKET_TODAY}T00:00:00Z`).getTime()

  for (const u of CHAIN_UNDERLYINGS) {
    const next = mulberry32(u.seed)
    const spot = UNDERLYINGS[u.symbol].price
    const underlyingChange = UNDERLYINGS[u.symbol].change
    const inc = strikeIncrement(spot)
    const atm = Math.round(spot / inc) * inc

    for (const expiration of CHAIN_EXPIRATIONS) {
      const dte = Math.round((new Date(`${expiration}T00:00:00Z`).getTime() - today) / 86_400_000)
      const t = dte / 365
      // Near-dated contracts carry most of the volume. Weighted by
      // position in the ladder rather than by date.
      const expiryWeight = [1, 0.55, 0.3][CHAIN_EXPIRATIONS.indexOf(expiration)]

      for (let i = -2; i <= 2; i++) {
        const strike = round2(atm + i * inc)
        // Moneyness in units of a one-standard-deviation move over the
        // contract's life, so the same number means the same thing on a
        // 14-day SPY call and a 70-day TSLA put.
        const sd0 = spot * u.baseIv * Math.sqrt(t)
        const m = (strike - spot) / sd0

        // The reported surface: a smirk, not a flat line. OTM puts bid
        // up, both wings above the money. Without it "highest IV" would
        // rank the six underlyings in order and say nothing about the
        // chain itself. Relative to baseIv rather than absolute, so a
        // 0.16 index and a 0.52 single name are skewed by the same
        // proportion instead of the same number of vol points.
        //
        // This is a *display* surface — the prices below are struck at
        // baseIv, flat across the ladder. That is deliberate and it is
        // not a shortcut: pricing off the smirk cannot keep the chain
        // monotone. Time value carries a factor g(m) = 1 + a(-m) + bm²,
        // and the slope of the put ladder just below the money works out
        // to -a, negative for any positive skew — so the 420 put prints
        // above the 430 beside it, which is an arbitrage rather than a
        // fixture. Pricing flat bounds the slope at 0.4·|m|·e^(-m²/2) <
        // 1, which is exactly the condition that keeps calls cheapening
        // and puts richening all the way up the ladder.
        //
        // Three decimals, not two: at two the smirk collapses into ties
        // and "highest IV" ranks equal contracts arbitrarily.
        const iv = Math.max(0.06, Math.round(u.baseIv * (1 + 0.07 * -m + 0.04 * m * m) * 1000) / 1000)

        for (const type of ['call', 'put'] as const) {
          // sd0, not the smirked iv — see the note above the surface.
          const sd = sd0
          const intrinsic = type === 'call' ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0)
          // Time value peaks at the money and decays into both wings. The
          // 0.4 coefficient keeps its slope well under 1, which is what
          // holds the chain monotone — calls cheapening as strikes rise and
          // puts richening, the way a real chain reads. A chain that fails
          // that is one no trader would believe.
          const timeValue = sd * 0.4 * Math.exp(-(m * m) / 2)
          const last = Math.max(0.01, round2(intrinsic + timeValue))

          // The day's move comes from the underlying's, scaled by delta. A
          // call on a name that fell should be down; drawing the change
          // independently produced chains where both sides rallied at once.
          const delta = type === 'call' ? callDelta(m) : callDelta(m) - 1
          // Dispersion *scales* the delta-driven move rather than adding to
          // it. Added, it flipped the sign wherever delta * change was
          // small — far OTM calls rallying on a down day, which made "top
          // gainers" a list of rounding errors.
          const drift = delta * underlyingChange * (0.78 + next() * 0.5)
          // Yesterday's close has to stay above zero, or the percentage is
          // a division by a negative price.
          const change = round2(Math.max(Math.min(drift, last - 0.01), -last * 4))
          const previousClose = round2(last - change)
          const changePct = previousClose <= 0 ? 0 : round2((change / previousClose) * 100)

          const activity = Math.exp(-(m * m) / 2)
          const volume = Math.round(120 + 26_000 * activity * expiryWeight * u.liquidity * (0.45 + next()))
          const openInterest = Math.round(volume * (1.6 + next() * 4.4) + 250)

          // Spreads widen away from the money and on the thinner names.
          // The wing contracts are where a modelled fill price is least
          // trustworthy and the table should show that, rather than hide it
          // behind a uniform penny spread.
          const halfSpread = Math.max(
            0.01,
            round2(last * 0.012 + 0.01 + ((1 - activity) * 0.06) / u.liquidity),
          )
          const bid = Math.min(last, Math.max(0.01, round2(last - halfSpread)))
          const ask = round2(last + halfSpread)

          contracts.push({
            symbol: u.symbol,
            strike,
            expiration,
            type,
            last,
            change,
            changePct,
            bid,
            ask,
            volume,
            openInterest,
            iv,
          })
        }
      }
    }
  }

  return contracts
}

/** 180 contracts: six underlyings, three expirations, five strikes, both
 * rights. Deep enough that pagination does real work and that the ranking
 * views disagree with each other — a five-row chain ranked six ways returns
 * the same five rows and proves nothing. */
export const OPTION_CHAIN: OptionContract[] = buildChain()

export interface StockQuote {
  symbol: string
  name: string
  price: number
  change: number
  changePct: number
  volume: number
  /** Billions of dollars, or **null for a fund**. An ETF has no market
   * capitalisation. Rendering that as 0 would sort SPY below every real
   * company and read as a fund worth nothing, so the column shows an em
   * dash and the ranking sorts nulls last rather than treating them as
   * zero. */
  marketCap: number | null
  /** First day of trading, YYYY-MM-DD — a date, not an instant. Dates are
   * approximate; they exist to give the "new listings" view something real
   * to sort on. */
  listedOn: string
}

/** Price is only carried here for names with no entry in UNDERLYINGS —
 * where a quote exists it wins, so the Markets table, the Activity rows and
 * the position charts all agree about the same stock. */
const STOCK_SEEDS: {
  symbol: string
  name: string
  price: number
  marketCap: number | null
  /** Average daily share volume. Carried per name rather than drawn from
   * one range, because a uniform draw made COST as busy as NVDA and turned
   * "most active" into a reshuffle of the same list. */
  avgVolume: number
  listedOn: string
}[] = [
  { symbol: 'AAPL', name: 'Apple Inc.', price: 232.4, marketCap: 3540, avgVolume: 52_000_000, listedOn: '1980-12-12' },
  { symbol: 'MSFT', name: 'Microsoft Corp.', price: 418.35, marketCap: 3110, avgVolume: 22_000_000, listedOn: '1986-03-13' },
  { symbol: 'NVDA', name: 'NVIDIA Corp.', price: 138.2, marketCap: 3390, avgVolume: 210_000_000, listedOn: '1999-01-22' },
  { symbol: 'AMZN', name: 'Amazon.com Inc.', price: 201.64, marketCap: 2120, avgVolume: 41_000_000, listedOn: '1997-05-15' },
  { symbol: 'GOOGL', name: 'Alphabet Inc. Class A', price: 176.28, marketCap: 2160, avgVolume: 28_000_000, listedOn: '2004-08-19' },
  { symbol: 'META', name: 'Meta Platforms Inc.', price: 562.91, marketCap: 1420, avgVolume: 15_000_000, listedOn: '2012-05-18' },
  { symbol: 'AVGO', name: 'Broadcom Inc.', price: 178.05, marketCap: 830, avgVolume: 24_000_000, listedOn: '2009-08-06' },
  { symbol: 'TSLA', name: 'Tesla Inc.', price: 238.1, marketCap: 760, avgVolume: 92_000_000, listedOn: '2010-06-29' },
  { symbol: 'LLY', name: 'Eli Lilly and Co.', price: 794.12, marketCap: 754, avgVolume: 3_400_000, listedOn: '1970-01-02' },
  { symbol: 'WMT', name: 'Walmart Inc.', price: 79.36, marketCap: 638, avgVolume: 18_000_000, listedOn: '1972-08-25' },
  { symbol: 'JPM', name: 'JPMorgan Chase & Co.', price: 221.47, marketCap: 623, avgVolume: 9_200_000, listedOn: '1969-03-05' },
  { symbol: 'UNH', name: 'UnitedHealth Group Inc.', price: 573.8, marketCap: 528, avgVolume: 4_100_000, listedOn: '1984-10-17' },
  { symbol: 'XOM', name: 'Exxon Mobil Corp.', price: 117.42, marketCap: 516, avgVolume: 16_000_000, listedOn: '1972-01-03' },
  { symbol: 'COST', name: 'Costco Wholesale Corp.', price: 884.19, marketCap: 392, avgVolume: 2_100_000, listedOn: '1985-12-05' },
  { symbol: 'HD', name: 'Home Depot Inc.', price: 368.55, marketCap: 366, avgVolume: 3_600_000, listedOn: '1981-09-22' },
  // Funds. Every one carries a null market cap on purpose — the column has
  // an em-dash branch and a fixture has to reach it.
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', price: 429.88, marketCap: null, avgVolume: 61_000_000, listedOn: '1993-01-22' },
  { symbol: 'QQQ', name: 'Invesco QQQ Trust', price: 372.4, marketCap: null, avgVolume: 34_000_000, listedOn: '1999-03-10' },
  { symbol: 'IWM', name: 'iShares Russell 2000 ETF', price: 218.63, marketCap: null, avgVolume: 28_000_000, listedOn: '2000-05-22' },
  { symbol: 'XLE', name: 'Energy Select Sector SPDR Fund', price: 91.24, marketCap: null, avgVolume: 17_000_000, listedOn: '1998-12-16' },
  { symbol: 'ARKK', name: 'ARK Innovation ETF', price: 54.77, marketCap: null, avgVolume: 12_000_000, listedOn: '2014-10-31' },
  // The recent end of the listing ladder, so "new listings" has something
  // to sort to the top and the view is not just the same table again.
  { symbol: 'ARM', name: 'Arm Holdings plc', price: 142.9, marketCap: 149, avgVolume: 8_400_000, listedOn: '2023-09-14' },
  { symbol: 'ALAB', name: 'Astera Labs Inc.', price: 87.35, marketCap: 14, avgVolume: 6_200_000, listedOn: '2024-03-20' },
  { symbol: 'RDDT', name: 'Reddit Inc.', price: 118.46, marketCap: 21, avgVolume: 9_800_000, listedOn: '2024-03-21' },
  { symbol: 'RBRK', name: 'Rubrik Inc.', price: 63.28, marketCap: 12, avgVolume: 3_100_000, listedOn: '2024-04-25' },
  { symbol: 'CRWV', name: 'CoreWeave Inc.', price: 96.14, marketCap: 47, avgVolume: 14_000_000, listedOn: '2025-03-28' },
  { symbol: 'CRCL', name: 'Circle Internet Group Inc.', price: 149.32, marketCap: 33, avgVolume: 11_000_000, listedOn: '2025-06-05' },
]

const stockRand = mulberry32(20262100)

/** 26 rows — two pages at 15, with enough spread in volume and market cap
 * that the five ranking views return visibly different tables. */
export const STOCKS: StockQuote[] = STOCK_SEEDS.map((s) => {
  // Both draws happen unconditionally. Drawing inside the branch below
  // would make the stream's order depend on which symbols have a quote, so
  // adding one underlying would silently shift every row after it.
  const drawPct = round2((stockRand() - 0.5) * 7)
  const volume = Math.round(s.avgVolume * (0.78 + stockRand() * 0.5))

  const quote = UNDERLYINGS[s.symbol]
  const price = quote ? quote.price : s.price
  const changePct = quote ? quote.changePct : drawPct
  const change = quote ? quote.change : round2(price - price / (1 + changePct / 100))

  return {
    symbol: s.symbol,
    name: s.name,
    price,
    change,
    changePct,
    volume,
    marketCap: s.marketCap,
    listedOn: s.listedOn,
  }
})

// ---------------------------------------------------------------------- //
// Strategies + LLM origination (Research page)
// ---------------------------------------------------------------------- //

export type StrategyStatus = 'draft' | 'backtest' | 'paper' | 'active' | 'retired'

export interface Strategy {
  id: string
  name: string
  version: number
  status: StrategyStatus
  backtest: { winRate: number; profitFactor: number; maxDrawdown: number; trades: number }
  /** An undifferentiated aggregate for now. PRD.md §8.1 scopes the
   * Dashboard's win rate to validated trades only, excluding LLM-originated
   * `unvalidated` ones (§6.2) — but that split belongs with the Research
   * origination panel (§8.5), which owns the validated/unvalidated
   * breakdown, and it lands when that page is built. Deferred deliberately;
   * don't nest the split in here from the Dashboard side. */
  live: { winRate: number; profitFactor: number; maxDrawdown: number; trades: number } | null
}

export const STRATEGIES: Strategy[] = [
  {
    id: 'strat-1',
    name: 'spx_mean_reversion',
    version: 4,
    status: 'active',
    backtest: { winRate: 68, profitFactor: 1.6, maxDrawdown: 14, trades: 312 },
    live: { winRate: 71, profitFactor: 1.5, maxDrawdown: 9, trades: 58 },
  },
  {
    id: 'strat-2',
    name: 'earnings_iv_crush',
    version: 2,
    status: 'paper',
    backtest: { winRate: 62, profitFactor: 1.3, maxDrawdown: 21, trades: 204 },
    live: { winRate: 59, profitFactor: 1.2, maxDrawdown: 12, trades: 31 },
  },
  {
    id: 'strat-3',
    name: 'sector_momentum',
    version: 1,
    status: 'backtest',
    backtest: { winRate: 55, profitFactor: 1.1, maxDrawdown: 28, trades: 187 },
    live: null,
  },
  {
    id: 'strat-4',
    name: 'vix_spike_fade',
    version: 3,
    status: 'draft',
    backtest: { winRate: 0, profitFactor: 0, maxDrawdown: 0, trades: 0 },
    live: null,
  },
]

export const LLM_ORIGINATION = {
  count: 34,
  winRate: 61,
  profitFactor: 1.35,
  standalonePnl: 1284.5,
  validated: 12,
  unvalidated: 22,
}

export const MARKET_PULSE = {
  vix: 16.8,
  sentimentComposite: SENTIMENT_COMPOSITE,
  topSector: 'Technology',
}

// ---------------------------------------------------------------------- //
// Account (Account page)
// ---------------------------------------------------------------------- //

export const ACCOUNT_SUMMARY = {
  cash: 12_480.32,
  buyingPower: 24_960.64,
  optionsBuyingPower: 12_480.32,
  settled: 11_200.0,
  unsettled: 1_280.32,
}

// ---------------------------------------------------------------------- //
// Dashboard header stats, per account (PRD.md §8.1)
// ---------------------------------------------------------------------- //

export interface Trend {
  changePct: number
  comparedTo: string
}

/** Everything that belongs to the *account* rather than to the strategy.
 * Paper and Cash are two different accounts holding different money, so the
 * Paper/Cash toggle has to move all of it — rendering paper's balance, or
 * paper's positions, while Cash is live misreports real money.
 *
 * Win rate is deliberately absent: it's a property of the selected
 * strategy, and it comes off STRATEGIES.live (see the Dashboard).
 *
 * Positions and activity are keyed here as of the Activity page. They were
 * previously a single shared set, which meant Flatten in either mode
 * cleared both books — fine while nothing rendered a per-account position
 * list, wrong the moment Activity did. Note the tradeoff PRD.md §3 calls
 * out: Activity has no Paper/Cash switch on it, so the header carries a
 * read-only account badge to say which book is on screen. */
export interface AccountSnapshot {
  portfolioHistory: PricePoint[]
  volume24h: number
  balanceTrend: Trend
  volumeTrend: Trend
  positions: Position[]
  activity: ActivityItem[]
  workingOrders: WorkingOrder[]
}

export const ACCOUNT_SNAPSHOTS: Record<AccountMode, AccountSnapshot> = {
  paper: {
    portfolioHistory: PAPER_HISTORY,
    volume24h: 18_420.55,
    balanceTrend: { changePct: 2.4, comparedTo: 'vs last 24h' },
    volumeTrend: { changePct: 15.2, comparedTo: 'vs last 24h' },
    positions: PAPER_POSITIONS,
    activity: PAPER_ACTIVITY,
    workingOrders: PAPER_WORKING_ORDERS,
  },
  cash: {
    portfolioHistory: CASH_HISTORY,
    volume24h: 4_860.2,
    balanceTrend: { changePct: -0.8, comparedTo: 'vs last 24h' },
    volumeTrend: { changePct: 6.3, comparedTo: 'vs last 24h' },
    positions: CASH_POSITIONS,
    activity: CASH_ACTIVITY,
    workingOrders: CASH_WORKING_ORDERS,
  },
}

export const ACCOUNT_LABEL: Record<AccountMode, string> = {
  paper: 'Paper',
  cash: 'Cash',
}

// ---------------------------------------------------------------------- //
// Risk limits + audit log (Settings page)
// ---------------------------------------------------------------------- //

export interface RiskLimit {
  key: string
  label: string
  value: number
  unit: '%' | 'count'
}

export const RISK_LIMITS: RiskLimit[] = [
  { key: 'max_risk_per_trade_pct', label: 'Max risk per trade', value: 7, unit: '%' },
  { key: 'max_daily_loss_pct', label: 'Max daily loss', value: 20, unit: '%' },
  { key: 'max_concurrent_positions', label: 'Max concurrent positions', value: 8, unit: 'count' },
  { key: 'max_exposure_per_underlying', label: 'Max exposure per underlying', value: 25, unit: '%' },
  { key: 'max_net_directional_pct', label: 'Max net directional exposure', value: 40, unit: '%' },
]

export interface AuditLogEntry {
  id: string
  time: string
  field: string
  previousValue: string
  newValue: string
}

export const AUDIT_LOG: AuditLogEntry[] = [
  { id: 'audit-1', time: '2026-07-15T14:00:00Z', field: 'max_risk_per_trade_pct', previousValue: '5', newValue: '7' },
  { id: 'audit-2', time: '2026-06-02T09:30:00Z', field: 'max_concurrent_positions', previousValue: '6', newValue: '8' },
]

// ---------------------------------------------------------------------- //
// Notifications + data sources + sentiment accuracy (Settings page)
// ---------------------------------------------------------------------- //

export interface NotificationRoute {
  event: string
  bell: boolean
  discord: boolean
}

export const NOTIFICATION_ROUTES: NotificationRoute[] = [
  { event: 'Order filled', bell: true, discord: true },
  { event: 'Order rejected', bell: true, discord: true },
  { event: 'Stop loss hit', bell: true, discord: true },
  { event: 'Daily loss halt', bell: true, discord: true },
  { event: 'Engine error / dead-man’s switch', bell: true, discord: true },
  { event: 'Price alert on a recommended trade', bell: true, discord: true },
  { event: 'New recommendations ready', bell: true, discord: false },
  { event: 'Strategy promotion eligible', bell: true, discord: false },
]

export interface DataSourceStatus {
  name: string
  status: 'connected' | 'degraded' | 'disconnected'
  detail: string
}

export const DATA_SOURCES: DataSourceStatus[] = [
  { name: 'Alpaca (execution + data)', status: 'connected', detail: 'Paper account, Basic plan' },
  { name: 'Finnhub (news + calendar)', status: 'connected', detail: 'Rate limit: 42/60 req/min' },
  { name: 'FRED (macro)', status: 'connected', detail: 'Daily refresh' },
  { name: 'StockTwits (social)', status: 'degraded', detail: 'Rate limited — retrying' },
]

export interface SentimentAccuracy {
  source: string
  tier: string
  accuracy1h: number
  accuracy1d: number
}

export const SENTIMENT_ACCURACY: SentimentAccuracy[] = [
  { source: 'Finnhub', tier: 'Provider-supplied', accuracy1h: 57, accuracy1d: 61 },
  { source: 'Alpaca', tier: 'Provider-supplied', accuracy1h: 55, accuracy1d: 58 },
  { source: 'Rules', tier: 'Deterministic patterns', accuracy1h: 63, accuracy1d: 66 },
  { source: 'LLM', tier: 'Tier 3', accuracy1h: 60, accuracy1d: 64 },
]
