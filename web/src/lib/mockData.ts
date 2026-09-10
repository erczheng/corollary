/**
 * Mock data for Phase 1 — Shell. Every shape here is written to match what
 * the real API will eventually return (see PRD.md §8 for the page specs
 * these back), so swapping a page from this file to a TanStack Query hook
 * in Phase 2 is a data-source change, not a component rewrite.
 *
 * Deterministic on purpose — a seeded PRNG, not Math.random() — so
 * screenshots and any future component tests don't flake between runs.
 */

import type {
  AccountMode,
  AccountSnapshot,
  ActivityAction,
  ActivityItem,
  ActivityStats,
  ActivityStatus,
  ApiKeyPresence,
  ArchivedChat,
  AuditLogEntry,
  CalendarEvent,
  ChainSpec,
  ChartRange,
  ChatScript,
  DataFeed,
  DataPlan,
  DataSourceStatus,
  NewsItem,
  Notification,
  NotificationRoute,
  OptionContract,
  Position,
  PricePoint,
  Recommendation,
  RiskLimit,
  SectorConsensus,
  Sentiment,
  SentimentAccuracy,
  SentimentComponent,
  SentimentTier,
  SocialAttentionItem,
  StockQuote,
  Strategy,
  UnderlyingQuote,
  WorkingOrder,
} from './types'
import { MACRO_SECTOR } from './types'

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

/** The session every fixture is written relative to.
 *
 * Days-to-expiry is measured from here rather than from the real clock, so
 * a fixture doesn't rot: tied to `new Date()`, a position written five
 * days from expiry silently becomes an expired one next week and the
 * near-expiry state stops being reachable. Phase 2 replaces this with the
 * market calendar's today. */
export const MARKET_TODAY = '2026-08-07'

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

/** A deposit is a cash movement, not an order — nothing was bought, sold,
 * filled or rejected. The Dashboard's Recent Executions is a trading feed
 * and shows orders only; Activity is the account's full ledger and shows
 * both (PRD.md §8.2). Keep the two readings apart here rather than
 * re-deriving the distinction at each call site. */
export function isOrderAction(action: ActivityAction): boolean {
  return action !== 'DEPOSIT' && action !== 'WITHDRAWAL'
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
 * multiplier off the contract rather than from this constant.
 *
 * Fixture default only — confirmed against the live Alpaca API, which
 * reports a contract's multiplier as a field on the contract itself. Real
 * code must read it from there, never reach for this constant. */
export const CONTRACT_MULTIPLIER = 100

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
/** A year of daily closes.
 *
 * Calendar days, not sessions — the walk skips weekends, so 400 of these
 * is about 286 closes reaching back to mid-2025.
 *
 * Long enough that every range on the Markets chart differs. At a quarter,
 * 3M / YTD / 1Y / All all returned the same points, which is four buttons
 * redrawing one chart; at a year, 1Y and All still did. Thirteen months
 * separates the last pair.
 *
 * Changing this does not move any quoted number: buildUnderlying walks
 * backwards from today's price, so previousClose comes from the first draw
 * and change and changePct follow it, whatever the count. */
const QUOTE_SESSIONS = 400

export const UNDERLYINGS: Record<string, UnderlyingQuote> = {
  AAPL: buildUnderlying('AAPL', 232.4, 20261001, QUOTE_SESSIONS),
  TSLA: buildUnderlying('TSLA', 238.1, 20261002, QUOTE_SESSIONS),
  SPY: buildUnderlying('SPY', 429.88, 20261003, QUOTE_SESSIONS),
  QQQ: buildUnderlying('QQQ', 372.4, 20261004, QUOTE_SESSIONS),
  MSFT: buildUnderlying('MSFT', 418.35, 20261005, QUOTE_SESSIONS),
  NVDA: buildUnderlying('NVDA', 138.2, 20261006, QUOTE_SESSIONS),
}

// ---------------------------------------------------------------------- //
// Working orders (Activity page)
// ---------------------------------------------------------------------- //

/** Paper carries one so the populated state is on screen; cash carries
 * none so the empty state is too. Both are reachable by toggling the
 * account rather than by contriving a sequence of clicks. */
const PAPER_WORKING_ORDERS: WorkingOrder[] = [
  {
    id: 'wo-1',
    positionId: 'pos-2',
    contractKey: null,
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

/** Names the feed writes about, with the sector each is filed under.
 *
 * Deliberately its own table rather than a field bolted onto `STOCK_SEEDS`.
 * The display names here are what a *headline* calls a company — "Apple",
 * not "Apple Inc." — so this is not a second copy of the stock universe's
 * legal names, and broad funds are absent because an index fund does not
 * report earnings or lose a CFO. */
const NEWS_UNIVERSE: { ticker: string; company: string; sector: string }[] = [
  { ticker: 'AAPL', company: 'Apple', sector: 'Technology' },
  { ticker: 'MSFT', company: 'Microsoft', sector: 'Technology' },
  { ticker: 'NVDA', company: 'Nvidia', sector: 'Technology' },
  { ticker: 'AVGO', company: 'Broadcom', sector: 'Technology' },
  { ticker: 'ARM', company: 'Arm', sector: 'Technology' },
  { ticker: 'ALAB', company: 'Astera Labs', sector: 'Technology' },
  { ticker: 'CRWV', company: 'CoreWeave', sector: 'Technology' },
  { ticker: 'RBRK', company: 'Rubrik', sector: 'Technology' },
  { ticker: 'GOOGL', company: 'Alphabet', sector: 'Communication Services' },
  { ticker: 'META', company: 'Meta', sector: 'Communication Services' },
  { ticker: 'RDDT', company: 'Reddit', sector: 'Communication Services' },
  { ticker: 'AMZN', company: 'Amazon', sector: 'Consumer Discretionary' },
  { ticker: 'TSLA', company: 'Tesla', sector: 'Consumer Discretionary' },
  { ticker: 'HD', company: 'Home Depot', sector: 'Consumer Discretionary' },
  { ticker: 'WMT', company: 'Walmart', sector: 'Consumer Staples' },
  { ticker: 'COST', company: 'Costco', sector: 'Consumer Staples' },
  { ticker: 'LLY', company: 'Eli Lilly', sector: 'Health Care' },
  { ticker: 'UNH', company: 'UnitedHealth', sector: 'Health Care' },
  { ticker: 'JPM', company: 'JPMorgan', sector: 'Financials' },
  { ticker: 'CRCL', company: 'Circle', sector: 'Financials' },
  { ticker: 'XOM', company: 'Exxon Mobil', sector: 'Energy' },
]

/** The outlets that actually reach this terminal. Alpaca's news endpoint is
 * Benzinga-sourced and Finnhub aggregates the rest. Neither vendor is named
 * here — a vendor is where an item *arrived from*, which is what `tier`
 * records, not who wrote it. */
const PUBLISHERS = ['Benzinga', 'Reuters', 'MarketWatch', 'Bloomberg', 'CNBC', 'Seeking Alpha']

const BANKS = ['Morgan Stanley', 'Goldman Sachs', 'Jefferies', 'Wedbush', 'Piper Sandler', 'BofA']

interface StoryContext {
  company: string
  ticker: string
  sector: string
  rand: () => number
}

interface Story {
  sentiment: Sentiment
  tier: SentimentTier
  /** Restricts a story to sectors where it is possible. An FDA hold on a
   * bank is not a rare event, it is a nonsense one. */
  sectors?: string[]
  headline: (ctx: StoryContext) => string
}

function pick<T>(rand: () => number, xs: T[]): T {
  return xs[Math.floor(rand() * xs.length)]
}

function int(rand: () => number, lo: number, hi: number): number {
  return lo + Math.floor(rand() * (hi - lo + 1))
}

/** Tier 2 is PRD.md §9's list of high-signal events, verbatim: beat/miss vs
 * estimates, guidance raised/cut, upgrade/downgrade, M&A, secondary
 * offering, buyback, executive departure, FDA action. The fixture is built
 * from that list rather than from invented headlines, so the tier column
 * means something — every `rules` story below is a pattern the
 * deterministic classifier genuinely claims to catch. */
const COMPANY_STORIES: Story[] = [
  // Tier 2 — deterministic patterns.
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) =>
      `${c.company} beats Q${int(c.rand, 1, 4)} estimates on ${pick(c.rand, ['services growth', 'data-centre demand', 'margin expansion', 'stronger unit volumes'])}`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) => `${c.company} misses Q${int(c.rand, 1, 4)} revenue estimates`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) => `${c.company} raises full-year guidance`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) =>
      `${c.company} cuts full-year guidance, citing ${pick(c.rand, ['softer demand', 'FX headwinds', 'a slower ramp', 'tariff exposure'])}`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) => `${pick(c.rand, BANKS)} upgrades ${c.ticker} to Buy from Hold`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) => `${pick(c.rand, BANKS)} downgrades ${c.ticker} to Hold from Buy`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) =>
      `${c.company} to acquire ${pick(c.rand, ['Halcyon Systems', 'Northbridge Labs', 'Veritas Compute', 'Lumen Analytics'])} in $${int(c.rand, 2, 18)}B deal`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) => `${c.company} announces $${int(c.rand, 1, 6)}B secondary offering`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) => `${c.company} board authorises $${int(c.rand, 5, 60)}B buyback`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) =>
      `${c.company} ${pick(c.rand, ['CFO', 'COO', 'chief revenue officer'])} departs after ${int(c.rand, 2, 11)} years`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    sectors: ['Health Care'],
    headline: (c) =>
      `FDA approves the ${c.company} ${pick(c.rand, ['obesity therapy', 'oncology combination', 'once-weekly formulation'])}`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    sectors: ['Health Care'],
    headline: (c) => `FDA places a clinical hold on the ${c.company} late-stage trial`,
  },

  // Tier 1 — the vendor shipped a score with the article.
  {
    sentiment: 'bullish',
    tier: 'provider',
    headline: (c) => `${c.company} named a top pick at ${pick(c.rand, BANKS)}`,
  },
  {
    sentiment: 'bearish',
    tier: 'provider',
    headline: (c) =>
      `${c.company} slips as ${pick(c.rand, ['peers guide lower', 'channel checks soften', 'a supplier warns'])}`,
  },
  {
    sentiment: 'neutral',
    tier: 'provider',
    headline: (c) => `${c.company} volume tops its ${int(c.rand, 20, 90)}-day average`,
  },
  {
    sentiment: 'bullish',
    tier: 'provider',
    headline: (c) => `${c.company} sets a fresh 52-week high`,
  },
  {
    sentiment: 'bearish',
    tier: 'provider',
    headline: (c) => `${c.company} touches a 52-week low in early trade`,
  },

  // Tier 3 — the LLM. Some come back under threshold, which is where
  // `unclassified` comes from and the only place it comes from.
  {
    sentiment: 'bullish',
    tier: 'llm',
    headline: (c) =>
      `${c.company} expands its ${pick(c.rand, ['cloud', 'silicon', 'logistics', 'payments'])} partnership with ${pick(c.rand, ['Accenture', 'Siemens', 'Oracle', 'Stripe'])}`,
  },
  {
    sentiment: 'bearish',
    tier: 'llm',
    headline: (c) =>
      `${c.company} faces ${pick(c.rand, ['an EU', 'an FTC', 'a DOJ', 'a state'])} inquiry over ${pick(c.rand, ['bundling', 'data handling', 'pricing practices'])}`,
  },
  {
    sentiment: 'neutral',
    tier: 'llm',
    headline: (c) =>
      `${c.company} reshuffles its ${pick(c.rand, ['hardware', 'international', 'enterprise'])} leadership`,
  },
  {
    sentiment: 'neutral',
    tier: 'llm',
    headline: (c) =>
      `${c.company} opens ${pick(c.rand, ['a Phoenix', 'an Austin', 'a Dublin', 'a Singapore'])} facility`,
  },
  {
    sentiment: 'unclassified',
    tier: 'llm',
    headline: (c) =>
      `Report: ${c.company} weighing ${pick(c.rand, ['a spin-off of its smaller unit', 'changes to its supplier terms', 'a shift in its capex plan'])}`,
  },
  {
    sentiment: 'unclassified',
    tier: 'llm',
    headline: (c) =>
      `${c.company} executives address ${pick(c.rand, ['margins', 'AI spend', 'capital return'])} at ${pick(c.rand, ['a Barclays', 'a Citi', 'a Deutsche Bank'])} conference`,
  },
  {
    sentiment: 'unclassified',
    tier: 'llm',
    headline: (c) => `${c.company} files an 8-K without further detail`,
  },
]

const MACRO_STORIES: Story[] = [
  {
    sentiment: 'neutral',
    tier: 'rules',
    headline: (c) =>
      `Fed holds rates, signals ${pick(c.rand, ['one cut', 'two cuts', 'no cuts'])} possible in 2027`,
  },
  {
    sentiment: 'bullish',
    tier: 'rules',
    headline: (c) =>
      `CPI prints ${(2 + c.rand() * 0.4).toFixed(1)}% against a ${(2.6 + c.rand() * 0.3).toFixed(1)}% consensus`,
  },
  {
    sentiment: 'bearish',
    tier: 'rules',
    headline: (c) => `Core PCE runs hotter than expected at ${(2.8 + c.rand() * 0.5).toFixed(1)}%`,
  },
  {
    sentiment: 'bullish',
    tier: 'provider',
    headline: (c) => `Nonfarm payrolls add ${int(c.rand, 180, 320)}K, above consensus`,
  },
  {
    sentiment: 'bearish',
    tier: 'provider',
    headline: (c) => `Jobless claims rise to ${int(c.rand, 232, 268)}K`,
  },
  {
    sentiment: 'neutral',
    tier: 'provider',
    headline: (c) => `Breadth narrows as ${int(c.rand, 3, 7)} names drive the session`,
  },
  {
    sentiment: 'bearish',
    tier: 'llm',
    headline: (c) =>
      `Treasury yields climb as ${pick(c.rand, ['auction demand softens', 'the term premium widens'])}`,
  },
  {
    sentiment: 'neutral',
    tier: 'llm',
    headline: (c) => `Oil holds a ${int(c.rand, 2, 6)}-session range ahead of the OPEC+ meeting`,
  },
  {
    sentiment: 'unclassified',
    tier: 'llm',
    headline: (c) =>
      `Officials offer mixed remarks on the ${pick(c.rand, ['September', 'October', 'December'])} path`,
  },
]

/** Sessions the feed reaches back over. Ten is deep enough that the
 * lookback control changes the answer at every step and that the sector and
 * publisher filters have something to cut, and shallow enough that the
 * corpus is still one plausible fortnight rather than an archive. */
const NEWS_SESSIONS = 10

/** Walks back one session at a time, skipping weekends.
 *
 * A real market calendar also skips holidays (CLAUDE.md), which Phase 2
 * takes from the actual one — the point here is only that the feed has no
 * Saturday headlines, which would be the first thing anyone noticed. */
function previousSessions(fromDate: string, count: number): string[] {
  const out: string[] = []
  const d = new Date(`${fromDate}T00:00:00Z`)
  while (out.length < count) {
    const day = d.getUTCDay()
    if (day !== 0 && day !== 6) out.push(d.toISOString().slice(0, 10))
    d.setUTCDate(d.getUTCDate() - 1)
  }
  return out
}

const newsRand = mulberry32(20264001)

/** One story, filed against a session and a time of day. */
function buildItem(id: string, date: string, rand: () => number): NewsItem {
  // 10:05 to 20:55 UTC — 6:05am to 4:55pm ET. News runs well before the
  // open and past the close, so the feed is not clipped to 09:30–16:00.
  const minutes = int(rand, 605, 1255)
  const hh = String(Math.floor(minutes / 60)).padStart(2, '0')
  const mm = String(minutes % 60).padStart(2, '0')
  const time = `${date}T${hh}:${mm}:00Z`

  if (rand() < 0.18) {
    const story = pick(rand, MACRO_STORIES)
    return {
      id,
      time,
      ticker: 'MARKET',
      headline: story.headline({
        company: 'the market',
        ticker: 'MARKET',
        sector: MACRO_SECTOR,
        rand,
      }),
      sentiment: story.sentiment,
      publisher: pick(rand, PUBLISHERS),
      sector: MACRO_SECTOR,
      tier: story.tier,
    }
  }

  const name = pick(rand, NEWS_UNIVERSE)
  const eligible = COMPANY_STORIES.filter(
    (s) => s.sectors === undefined || s.sectors.includes(name.sector),
  )
  const story = pick(rand, eligible)
  return {
    id,
    time,
    ticker: name.ticker,
    headline: story.headline({ ...name, rand }),
    sentiment: story.sentiment,
    publisher: pick(rand, PUBLISHERS),
    sector: name.sector,
    tier: story.tier,
  }
}

function buildNewsFeed(): NewsItem[] {
  const items: NewsItem[] = []

  previousSessions(MARKET_TODAY, NEWS_SESSIONS).forEach((date, sessionIndex) => {
    // The newest session runs shorter because it is still in progress — the
    // top of the feed is "so far today", not a finished day.
    const count = sessionIndex === 0 ? 9 : int(newsRand, 9, 11)
    for (let i = 0; i < count; i += 1) {
      items.push(buildItem(`news-${date}-${i}`, date, newsRand))
    }
  })

  // Newest first, which is the order the page opens in and the order the
  // store prepends against.
  return items.sort((a, b) => b.time.localeCompare(a.time))
}

export const NEWS_ITEMS: NewsItem[] = buildNewsFeed()

/** Headlines that arrive *while the page is open*.
 *
 * Held apart from the published corpus rather than generated on the fly, so
 * a session still replays identically — the store releases these in order,
 * and what a reader sees at the third poll is the same on every run.
 *
 * When the reserve is exhausted the feed simply stops growing, which is
 * what a quiet afternoon looks like. No news is the ordinary case on this
 * page, so a poll that returns nothing is still a *successful* poll and the
 * status pill has to keep saying so — unlike a price stream, where silence
 * means something has broken. */
export const NEWS_INCOMING: NewsItem[] = (() => {
  const rand = mulberry32(20264002)
  // `time` is assigned by the store on release, against the fixture's
  // clock rather than the wall clock — see `pollNews`.
  return Array.from({ length: 12 }, (_, i) =>
    buildItem(`news-incoming-${i}`, MARKET_TODAY, rand),
  )
})()

// ---------------------------------------------------------------------- //
// Market sentiment composite (News page)
// ---------------------------------------------------------------------- //

/** The seven components of PRD.md §8.3, mirroring CNN's published
 * methodology. Equally weighted, so the headline number is their mean —
 * `compositeScore` in `lib/news.ts` derives it rather than storing it. A
 * stored composite is a number that can drift from the breakdown printed
 * directly underneath it. */
export const SENTIMENT_COMPONENTS: SentimentComponent[] = [
  { name: 'Momentum', description: 'S&P 500 against its 125-day moving average', score: 64 },
  { name: 'Strength', description: '52-week highs against 52-week lows', score: 55 },
  { name: 'Breadth', description: 'Advancing against declining volume', score: 51 },
  { name: 'Put/call ratio', description: 'CBOE equity put/call, 5-day average', score: 60 },
  { name: 'Volatility', description: 'VIX against its 50-day moving average', score: 62 },
  { name: 'Safe-haven demand', description: '20-day equity return minus Treasury return', score: 57 },
  { name: 'Junk bond demand', description: 'High-yield spread, FRED BAMLH0A0HYM2', score: 57 },
]

/** When the composite was last computed. Daily, after the close: every
 * input is a daily series, so a figure restamped every minute would claim a
 * freshness it does not have. This is why the live pill on this page covers
 * the *feed* and nothing else. */
export const SENTIMENT_AS_OF = '2026-08-07T20:15:00Z'

/** The prior session's composite, for the one-day delta. Stored rather than
 * derived because yesterday's seven components are not carried — the
 * breakdown on screen is today's, and reconstructing a second one would be
 * inventing data to fill a column. */
export const SENTIMENT_PREVIOUS = 51

// ---------------------------------------------------------------------- //
// Social attention (News page)
// ---------------------------------------------------------------------- //

export const SOCIAL_ATTENTION: SocialAttentionItem[] = [
  { ticker: 'CRWV', mentions: 6180, baselineMentions: 1240, labeledCount: 2410, sampleSize: 6180, sentiment: 'bullish' },
  { ticker: 'NVDA', mentions: 4820, baselineMentions: 2100, labeledCount: 1740, sampleSize: 4820, sentiment: 'bullish' },
  { ticker: 'TSLA', mentions: 3910, baselineMentions: 3400, labeledCount: 1390, sampleSize: 3910, sentiment: 'bearish' },
  { ticker: 'RDDT', mentions: 2960, baselineMentions: 1180, labeledCount: 905, sampleSize: 2960, sentiment: 'bullish' },
  { ticker: 'AAPL', mentions: 2240, baselineMentions: 2000, labeledCount: 820, sampleSize: 2240, sentiment: 'bullish' },
  // Below its own baseline. Attention *falling* is a reading too, and a
  // panel where every row is up is a leaderboard rather than a measurement.
  { ticker: 'SPY', mentions: 1650, baselineMentions: 1700, labeledCount: 540, sampleSize: 1650, sentiment: 'neutral' },
  { ticker: 'XOM', mentions: 940, baselineMentions: 1450, labeledCount: 310, sampleSize: 940, sentiment: 'bearish' },
  // Thin label coverage — 88 of 780. A direction is computable and the
  // sample is too small to publish one, which is what `unclassified` is
  // for here just as it is in the feed.
  { ticker: 'ALAB', mentions: 780, baselineMentions: 260, labeledCount: 88, sampleSize: 780, sentiment: 'unclassified' },
]

export const SOCIAL_AS_OF = '2026-08-07T20:00:00Z'

// ---------------------------------------------------------------------- //
// Sector consensus (News page — "Top rated by sector")
// ---------------------------------------------------------------------- //

/** All eleven GICS sectors, refreshed monthly from Finnhub (PRD.md §8.3).
 * Every row carries the same as-of date because they arrive in one job — a
 * per-row date would imply a staggered refresh that does not happen. */
export const SECTOR_CONSENSUS: SectorConsensus[] = [
  { sector: 'Communication Services', etf: 'XLC', leader: 'GOOGL', buy: 71, hold: 24, sell: 5, asOf: '2026-08-01' },
  { sector: 'Technology', etf: 'XLK', leader: 'AAPL', buy: 68, hold: 27, sell: 5, asOf: '2026-08-01' },
  { sector: 'Consumer Discretionary', etf: 'XLY', leader: 'AMZN', buy: 64, hold: 29, sell: 7, asOf: '2026-08-01' },
  { sector: 'Health Care', etf: 'XLV', leader: 'LLY', buy: 61, hold: 32, sell: 7, asOf: '2026-08-01' },
  { sector: 'Industrials', etf: 'XLI', leader: 'CAT', buy: 58, hold: 35, sell: 7, asOf: '2026-08-01' },
  { sector: 'Financials', etf: 'XLF', leader: 'BRK.B', buy: 55, hold: 38, sell: 7, asOf: '2026-08-01' },
  { sector: 'Utilities', etf: 'XLU', leader: 'NEE', buy: 52, hold: 40, sell: 8, asOf: '2026-08-01' },
  { sector: 'Consumer Staples', etf: 'XLP', leader: 'WMT', buy: 49, hold: 43, sell: 8, asOf: '2026-08-01' },
  { sector: 'Energy', etf: 'XLE', leader: 'XOM', buy: 47, hold: 41, sell: 12, asOf: '2026-08-01' },
  { sector: 'Materials', etf: 'XLB', leader: 'LIN', buy: 44, hold: 45, sell: 11, asOf: '2026-08-01' },
  // The one sector the street is net-cautious on. A panel where every row
  // is a majority Buy is a panel nobody needs to read.
  { sector: 'Real Estate', etf: 'XLRE', leader: 'PLD', buy: 33, hold: 49, sell: 18, asOf: '2026-08-01' },
]

// ---------------------------------------------------------------------- //
// Market calendar (News page)
// ---------------------------------------------------------------------- //

/** Forward-looking only, per PRD.md §8.3 — a calendar of what has not
 * happened yet. `upcomingEvents` in `lib/news.ts` enforces that against the
 * clock rather than trusting the fixture to stay ahead of it.
 *
 * Three weeks out, which reaches past the near expirations in
 * `CHAIN_EXPIRATIONS`: the point of the panel is seeing the event risk that
 * sits inside a contract you are already holding. */
export const CALENDAR_EVENTS: CalendarEvent[] = [
  { id: 'cal-1', date: '2026-08-10', at: '2026-08-10T14:00:00Z', type: 'economic', title: 'Wholesale inventories' },
  { id: 'cal-2', date: '2026-08-10', at: '2026-08-10T20:05:00Z', type: 'earnings', title: 'Q2 earnings call', ticker: 'RBRK' },
  { id: 'cal-3', date: '2026-08-11', at: '2026-08-11T12:30:00Z', type: 'economic', title: 'CPI, July' },
  { id: 'cal-4', date: '2026-08-11', at: '2026-08-11T18:00:00Z', type: 'central-bank', title: 'FOMC rate decision' },
  { id: 'cal-5', date: '2026-08-11', at: '2026-08-11T18:30:00Z', type: 'central-bank', title: 'Fed chair press conference' },
  // No time at all. An ex-dividend date is a property of the session, and
  // the row reads "All day" rather than inventing an 8:00 PM.
  { id: 'cal-6', date: '2026-08-12', at: null, type: 'dividend', title: 'Ex-dividend date', ticker: 'JNJ' },
  { id: 'cal-7', date: '2026-08-12', at: '2026-08-12T12:30:00Z', type: 'economic', title: 'PPI, July' },
  { id: 'cal-8', date: '2026-08-13', at: null, type: 'geopolitical', title: 'EU trade council session' },
  { id: 'cal-9', date: '2026-08-13', at: '2026-08-13T20:05:00Z', type: 'earnings', title: 'Q3 earnings call', ticker: 'AAPL' },
  { id: 'cal-10', date: '2026-08-14', at: '2026-08-14T12:30:00Z', type: 'economic', title: 'Retail sales, July' },
  { id: 'cal-11', date: '2026-08-17', at: '2026-08-17T20:05:00Z', type: 'earnings', title: 'Q2 earnings call', ticker: 'HD' },
  { id: 'cal-12', date: '2026-08-18', at: null, type: 'dividend', title: 'Ex-dividend date', ticker: 'XOM' },
  { id: 'cal-13', date: '2026-08-19', at: '2026-08-19T18:00:00Z', type: 'central-bank', title: 'FOMC minutes' },
  { id: 'cal-14', date: '2026-08-20', at: '2026-08-20T20:20:00Z', type: 'earnings', title: 'Q2 earnings call', ticker: 'NVDA' },
  { id: 'cal-15', date: '2026-08-21', at: '2026-08-21T13:00:00Z', type: 'geopolitical', title: 'G20 finance ministers meet' },
  { id: 'cal-16', date: '2026-08-25', at: '2026-08-25T14:00:00Z', type: 'economic', title: 'Consumer confidence' },
]

// ---------------------------------------------------------------------- //
// Option chains + stocks/ETFs (Markets page)
// ---------------------------------------------------------------------- //

/** The three expirations the chain is built over — all Fridays, which is
 * when standard US equity options actually expire. Forward-dated from
 * MARKET_TODAY at 14 / 42 / 70 days so the near, middle and far ends of
 * the curve are all on screen and time value visibly decays across them. */
export const CHAIN_EXPIRATIONS = ['2026-08-21', '2026-09-18', '2026-10-16']

/** Underlyings with a listed chain.
 *
 * Spot is deliberately *not* repeated here — it is read from the quote map,
 * so the Markets chain and the Activity position rows cannot disagree about
 * what AAPL costs. Exported because the poll re-prices the chain from the
 * same constants the fixture was built with. */
export const CHAIN_SPECS: ChainSpec[] = [
  { symbol: 'SPY', baseIv: 0.16, liquidity: 3.2, seed: 20262001 },
  { symbol: 'AAPL', baseIv: 0.28, liquidity: 1.4, seed: 20262002 },
  { symbol: 'NVDA', baseIv: 0.44, liquidity: 1.6, seed: 20262003 },
  { symbol: 'TSLA', baseIv: 0.52, liquidity: 1.1, seed: 20262004 },
  { symbol: 'QQQ', baseIv: 0.19, liquidity: 0.9, seed: 20262005 },
  { symbol: 'MSFT', baseIv: 0.24, liquidity: 0.35, seed: 20262006 },
]

export const CHAIN_SPEC_BY_SYMBOL: Record<string, ChainSpec> = Object.fromEntries(
  CHAIN_SPECS.map((s) => [s.symbol, s]),
)

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
 * near 0. */
export function callDelta(moneyness: number): number {
  return 1 / (1 + Math.exp(moneyness * 1.55))
}

export function chainDte(expiration: string): number {
  return Math.round(
    (Date.parse(`${expiration}T00:00:00Z`) - Date.parse(`${MARKET_TODAY}T00:00:00Z`)) / 86_400_000,
  )
}

/** Standardised moneyness — the strike's distance from spot in units of a
 * one-standard-deviation move over the contract's life. The same number
 * means the same thing on a 14-day SPY call and a 70-day TSLA put. */
export function moneyness(spot: number, strike: number, dte: number, baseIv: number): number {
  const sd = spot * baseIv * Math.sqrt(dte / 365)
  return sd === 0 ? 0 : (strike - spot) / sd
}

/** What one contract is worth at a given spot.
 *
 * Struck at the underlying's **base** vol, flat across the ladder, and that
 * is deliberate rather than a shortcut. With a skew factor g(m) in time
 * value, the slope of the put ladder just below the money works out to -a
 * for skew slope a — so any positive skew prints a lower strike above the
 * one beside it, which is an arbitrage rather than a fixture. Pricing flat
 * bounds the slope at 0.4·|m|·e^(-m²/2) < 1, which is exactly the condition
 * that keeps calls cheapening and puts richening all the way up.
 *
 * Exported because the market poll re-prices the whole chain through this
 * one function. Re-marking each contract with an independent random walk
 * would invert the ladder within seconds — a chain moves because its
 * underlying moved, not because each strike wandered off on its own. */
export function priceContract(
  spot: number,
  strike: number,
  dte: number,
  baseIv: number,
  type: 'call' | 'put',
): number {
  const m = moneyness(spot, strike, dte, baseIv)
  const sd = spot * baseIv * Math.sqrt(dte / 365)
  const intrinsic = type === 'call' ? Math.max(spot - strike, 0) : Math.max(strike - spot, 0)
  const timeValue = sd * 0.4 * Math.exp(-(m * m) / 2)
  return Math.max(0.01, round2(intrinsic + timeValue))
}

/** Half the quoted spread. Widens away from the money and on the thinner
 * names — the wing contracts are where a modelled fill price is least
 * trustworthy, and the table should show that rather than hide it behind a
 * uniform penny spread. */
export function halfSpread(last: number, m: number, liquidity: number): number {
  const activity = Math.exp(-(m * m) / 2)
  return Math.max(0.01, round2(last * 0.012 + 0.01 + ((1 - activity) * 0.06) / liquidity))
}

/** The reported vol surface: a smirk, not a flat line. OTM puts bid up,
 * both wings above the money. Without it "highest IV" would rank the six
 * underlyings in order and say nothing about the chain itself.
 *
 * Relative to baseIv rather than absolute, so a 0.16 index and a 0.52
 * single name are skewed by the same proportion instead of the same number
 * of vol points. Smooth in strike with no draw in it: a real surface is
 * smooth, and independent per-strike noise inverted the far-dated ladders.
 *
 * Three decimals, not two — at two the smirk collapses into ties and
 * "highest IV" ranks equal contracts arbitrarily. */
export function surfaceIv(baseIv: number, m: number): number {
  return Math.max(0.06, Math.round(baseIv * (1 + 0.07 * -m + 0.04 * m * m) * 1000) / 1000)
}

function buildChain(): OptionContract[] {
  const contracts: OptionContract[] = []

  for (const u of CHAIN_SPECS) {
    const next = mulberry32(u.seed)
    const spot = UNDERLYINGS[u.symbol].price
    const underlyingChange = UNDERLYINGS[u.symbol].change
    const inc = strikeIncrement(spot)
    const atm = Math.round(spot / inc) * inc

    for (const expiration of CHAIN_EXPIRATIONS) {
      const dte = chainDte(expiration)
      // Near-dated contracts carry most of the volume. Weighted by position
      // in the ladder rather than by date.
      const expiryWeight = [1, 0.55, 0.3][CHAIN_EXPIRATIONS.indexOf(expiration)]

      for (let i = -2; i <= 2; i++) {
        const strike = round2(atm + i * inc)
        const m = moneyness(spot, strike, dte, u.baseIv)
        const iv = surfaceIv(u.baseIv, m)

        for (const type of ['call', 'put'] as const) {
          const last = priceContract(spot, strike, dte, u.baseIv, type)

          // The day's move comes from the underlying's, scaled by delta. A
          // call on a name that fell should be down; drawing the change
          // independently produced chains where both sides rallied at once.
          // The dispersion *scales* that move rather than adding to it —
          // added, it flipped the sign wherever delta was small.
          const delta = type === 'call' ? callDelta(m) : callDelta(m) - 1
          const drift = delta * underlyingChange * (0.78 + next() * 0.5)
          // Yesterday's close has to stay above zero, or the percentage is
          // a division by a negative price.
          const change = round2(Math.max(Math.min(drift, last - 0.01), -last * 4))
          const previousClose = round2(last - change)

          const activity = Math.exp(-(m * m) / 2)
          const volume = Math.round(120 + 26_000 * activity * expiryWeight * u.liquidity * (0.45 + next()))
          const openInterest = Math.round(volume * (1.6 + next() * 4.4) + 250)

          const half = halfSpread(last, m, u.liquidity)

          contracts.push({
            symbol: u.symbol,
            strike,
            expiration,
            type,
            last,
            previousClose,
            change,
            changePct: round2((change / previousClose) * 100),
            bid: Math.min(last, Math.max(0.01, round2(last - half))),
            ask: round2(last + half),
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
 * the same five rows and proves nothing.
 *
 * This is the *opening* snapshot. The live chain lives in the store, which
 * re-prices it from the underlying on every poll. */
export const OPTION_CHAIN: OptionContract[] = buildChain()

/** Metadata, and the price each name is seeded at. Nothing here is a live
 * value — the quote map below owns those, for every symbol on the page. */
const STOCK_SEEDS: {
  symbol: string
  name: string
  price: number
  avgVolume: number
  marketCap: number | null
  seed: number
}[] = [
  { symbol: 'AAPL', name: 'Apple Inc.', price: 232.4, avgVolume: 52_000_000, marketCap: 3540, seed: 20263001 },
  { symbol: 'MSFT', name: 'Microsoft Corp.', price: 418.35, avgVolume: 22_000_000, marketCap: 3110, seed: 20263002 },
  { symbol: 'NVDA', name: 'NVIDIA Corp.', price: 138.2, avgVolume: 210_000_000, marketCap: 3390, seed: 20263003 },
  { symbol: 'AMZN', name: 'Amazon.com Inc.', price: 201.64, avgVolume: 41_000_000, marketCap: 2120, seed: 20263004 },
  { symbol: 'GOOGL', name: 'Alphabet Inc. Class A', price: 176.28, avgVolume: 28_000_000, marketCap: 2160, seed: 20263005 },
  { symbol: 'META', name: 'Meta Platforms Inc.', price: 562.91, avgVolume: 15_000_000, marketCap: 1420, seed: 20263006 },
  { symbol: 'AVGO', name: 'Broadcom Inc.', price: 178.05, avgVolume: 24_000_000, marketCap: 830, seed: 20263007 },
  { symbol: 'TSLA', name: 'Tesla Inc.', price: 238.1, avgVolume: 92_000_000, marketCap: 760, seed: 20263008 },
  { symbol: 'LLY', name: 'Eli Lilly and Co.', price: 794.12, avgVolume: 3_400_000, marketCap: 754, seed: 20263009 },
  { symbol: 'WMT', name: 'Walmart Inc.', price: 79.36, avgVolume: 18_000_000, marketCap: 638, seed: 20263010 },
  { symbol: 'JPM', name: 'JPMorgan Chase & Co.', price: 221.47, avgVolume: 9_200_000, marketCap: 623, seed: 20263011 },
  { symbol: 'UNH', name: 'UnitedHealth Group Inc.', price: 573.8, avgVolume: 4_100_000, marketCap: 528, seed: 20263012 },
  { symbol: 'XOM', name: 'Exxon Mobil Corp.', price: 117.42, avgVolume: 16_000_000, marketCap: 516, seed: 20263013 },
  { symbol: 'COST', name: 'Costco Wholesale Corp.', price: 884.19, avgVolume: 2_100_000, marketCap: 392, seed: 20263014 },
  { symbol: 'HD', name: 'Home Depot Inc.', price: 368.55, avgVolume: 3_600_000, marketCap: 366, seed: 20263015 },
  // Funds. Every one carries a null market cap on purpose — the column has
  // an em-dash branch and a fixture has to reach it.
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF Trust', price: 429.88, avgVolume: 61_000_000, marketCap: null, seed: 20263016 },
  { symbol: 'QQQ', name: 'Invesco QQQ Trust', price: 372.4, avgVolume: 34_000_000, marketCap: null, seed: 20263017 },
  { symbol: 'IWM', name: 'iShares Russell 2000 ETF', price: 218.63, avgVolume: 28_000_000, marketCap: null, seed: 20263018 },
  { symbol: 'XLE', name: 'Energy Select Sector SPDR Fund', price: 91.24, avgVolume: 17_000_000, marketCap: null, seed: 20263019 },
  { symbol: 'ARKK', name: 'ARK Innovation ETF', price: 54.77, avgVolume: 12_000_000, marketCap: null, seed: 20263020 },
  { symbol: 'ARM', name: 'Arm Holdings plc', price: 142.9, avgVolume: 8_400_000, marketCap: 149, seed: 20263021 },
  { symbol: 'ALAB', name: 'Astera Labs Inc.', price: 87.35, avgVolume: 6_200_000, marketCap: 14, seed: 20263022 },
  { symbol: 'RDDT', name: 'Reddit Inc.', price: 118.46, avgVolume: 9_800_000, marketCap: 21, seed: 20263023 },
  { symbol: 'RBRK', name: 'Rubrik Inc.', price: 63.28, avgVolume: 3_100_000, marketCap: 12, seed: 20263024 },
  { symbol: 'CRWV', name: 'CoreWeave Inc.', price: 96.14, avgVolume: 14_000_000, marketCap: 47, seed: 20263025 },
  { symbol: 'CRCL', name: 'Circle Internet Group Inc.', price: 149.32, avgVolume: 11_000_000, marketCap: 33, seed: 20263026 },
]

/** **One price per symbol, for the whole terminal.**
 *
 * UNDERLYINGS covers the six names a position can be written on. This
 * extends it to every symbol the Markets page lists, and it is a superset
 * rather than a second map on purpose: two maps would let the Markets table
 * and an Activity row show different prices for the same stock, which is
 * the exact failure `Position.underlying` being keyed by symbol exists to
 * prevent. The store seeds its live quotes from here and both pages read
 * that one map.
 *
 * The stream and the poll are still different things — the stream is capped
 * at 30 symbols and scoped to open positions, while polled snapshots are
 * bounded by a request budget instead. They write to the same prices. */
export const MARKET_QUOTES: Record<string, UnderlyingQuote> = {
  ...UNDERLYINGS,
  ...Object.fromEntries(
    STOCK_SEEDS.filter((s) => !(s.symbol in UNDERLYINGS)).map((s) => [
      s.symbol,
      buildUnderlying(s.symbol, s.price, s.seed, QUOTE_SESSIONS),
    ]),
  ),
}

const stockRand = mulberry32(20262100)

/** 26 rows — two pages at 15, with enough spread in volume, market cap and
 * relative volume that the five ranking views return visibly different
 * tables.
 *
 * Price, change and percent are read from the quote map rather than stored
 * here, so there is nothing to keep in sync. */
export const STOCKS: StockQuote[] = STOCK_SEEDS.map((s) => {
  const quote = MARKET_QUOTES[s.symbol]
  // Today's volume against the average. Most names trade near their usual
  // size; a few are having a day, which is what the trending screen is for.
  const relative = stockRand() < 0.22 ? 1.9 + stockRand() * 2.6 : 0.55 + stockRand() * 0.85

  return {
    symbol: s.symbol,
    name: s.name,
    price: quote.price,
    change: quote.change,
    changePct: quote.changePct,
    volume: Math.round(s.avgVolume * relative),
    avgVolume: s.avgVolume,
    marketCap: s.marketCap,
  }
})

// ---------------------------------------------------------------------- //
// Strategies + LLM origination (Research page)
// ---------------------------------------------------------------------- //

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

/** The composite is deliberately **not** a field here. It is the mean of
 * `SENTIMENT_COMPONENTS`, and `compositeScore` in `lib/news.ts` derives it
 * — a copy stored on this object is a number that can disagree with the
 * breakdown the News page prints underneath it, which is the same failure
 * `activityStats` exists to avoid one page over. Research reads it from
 * `news.ts` when it is built. */
export const MARKET_PULSE = {
  vix: 16.8,
  /** Yesterday's close, so the day's change is **derived** on the page
   * rather than stored beside the level.
   *
   * Same reasoning as the sentiment composite: a stored change is a second
   * number that can disagree with the two it sits between. Above the level,
   * so the fixture exercises a *falling* VIX — the direction that would be
   * coloured green by a naive trend line, which is exactly the case worth
   * having on screen. */
  vixPreviousClose: 17.25,
  topSector: 'Technology',
}

// ---------------------------------------------------------------------- //
// Research chat (Research page, PRD.md §8.5)
// ---------------------------------------------------------------------- //

export const CHAT_SCRIPT: ChatScript[] = [
  {
    match: /strateg|propose|idea for a|build me/,
    reply:
      'Based on this account’s fills, mean-reversion entries on oversold index ETFs have carried the strongest live win rate, and the losing tail is concentrated in trades held through an earnings print. Here is a proposal that keeps the entry and adds a time stop before earnings.',
    proposal: {
      name: 'index_mean_reversion_v2',
      summary:
        'Mean reversion on index ETFs, exiting before any earnings event in the underlying basket.',
      rules: [
        'Enter when RSI(14) < 30 and price is below the 20-day SMA',
        'Put credit spread, 30–45 DTE, short strike at 0.30 delta',
        'Profit target 50% of credit, stop at 200% of credit',
        'Time stop 2 DTE, and exit any position before an earnings date',
      ],
    },
  },
  {
    match: /recommend|candidate|what should i|trade idea|scanner/,
    reply:
      'Seven candidates cleared the scanner for this session. The highest base rate is the SPY put credit spread at 71% — that figure is the backtested hit rate for its setup class, not a model’s confidence. One candidate is an LLM origination with no setup match, so it is capped at a third of normal size and labelled Untested.',
    proposal: null,
  },
  {
    match: /vix|volatilit|market|macro|sentiment/,
    reply:
      'VIX is 16.8, which is unremarkable. The in-house sentiment composite is derived from its seven components rather than stored, so the number beside it on the News page is always the mean of what is printed underneath. Technology leads the session.',
    proposal: null,
  },
  {
    match: /risk|limit|size|position siz|exposure/,
    reply:
      'Per-trade risk is capped at 7% of equity, and what "risk" means depends on the structure: maximum loss at expiry for a defined-risk spread, premium paid for a long option, and a ±2σ stress loss for anything undefined. The engine enforces all five ceilings server-side — the numbers on Settings are what is stored, not what is allowed.',
    proposal: null,
  },
  {
    match: /backtest|history|historical/,
    reply:
      'Alpaca’s options history starts February 2024, and in practice it is bars only — there is no historical quotes endpoint, and trades reach back just seven days. So every backtest fill price is an estimate from the spread model rather than a measurement, and results say so alongside the assumption used.',
    proposal: null,
  },
]

/** Shown when nothing matches. Says it is a shell rather than improvising —
 * an evasive-but-fluent non-answer is exactly the failure mode this panel
 * should not have. */
export const CHAT_FALLBACK =
  'This is a scripted shell, so I have no reply for that yet. The live model arrives with the LLM layer in Phase 4. Try asking about strategies, recommendations, market context, risk limits, or backtests.'

/** Starting prompts, so the routing is discoverable rather than guesswork.
 * The chat opens empty on purpose — that makes the designed empty state the
 * default view and the populated transcript one click away, so both are
 * reachable without contriving a sequence. */
export const CHAT_SUGGESTIONS: string[] = [
  'Propose a strategy from this account’s history',
  'What are today’s recommendations?',
  'How is my risk configured?',
]

/** Seeded history, so the menu has something in it on a cold start.
 *
 * The live transcript still opens empty — that empty state is deliberate
 * (see `CHAT_SUGGESTIONS`) and this does not disturb it. What it does is
 * make the *populated* history reachable without first having to hold a
 * conversation, which is the same reason every other fixture here covers
 * the states a component can render rather than only the interesting one.
 */
export const CHAT_HISTORY: ArchivedChat[] = [
  {
    id: 'chat-2026-08-07-a',
    title: 'How is my risk configured?',
    at: '2026-08-07T14:22:00Z',
    messages: [
      {
        id: 'chat-h1-m1',
        role: 'user',
        text: 'How is my risk configured?',
        at: '2026-08-07T14:21:00Z',
        proposal: null,
      },
      {
        id: 'chat-h1-m2',
        role: 'assistant',
        text: 'Max risk per trade is 7% of equity, max daily loss 20%, and you can hold 8 concurrent positions. Exposure to any one underlying is capped at 25% and net directional at 40%. The engine enforces all five server-side — Settings edits what is stored, not what is allowed.',
        at: '2026-08-07T14:21:00Z',
        proposal: null,
      },
      {
        id: 'chat-h1-m3',
        role: 'user',
        text: 'What counts as risk on a naked short?',
        at: '2026-08-07T14:22:00Z',
        proposal: null,
      },
      {
        id: 'chat-h1-m4',
        role: 'assistant',
        text: 'An undefined-risk structure is sized against a stress loss at ±2σ of the underlying’s 20-day realized volatility. Defined-risk structures use maximum loss at expiry, and a long option uses the premium paid.',
        at: '2026-08-07T14:22:00Z',
        proposal: null,
      },
    ],
  },
  {
    id: 'chat-2026-08-06-a',
    title: 'What are today’s recommendations?',
    at: '2026-08-06T17:40:00Z',
    messages: [
      {
        id: 'chat-h2-m1',
        role: 'user',
        text: 'What are today’s recommendations?',
        at: '2026-08-06T17:40:00Z',
        proposal: null,
      },
      {
        id: 'chat-h2-m2',
        role: 'assistant',
        text: 'Seven candidates are on the board. Five carry a backtested base rate, one is an LLM origination with no setup match — capped at a third of normal size — and one is a new setup class with too few samples to quote a rate at all.',
        at: '2026-08-06T17:40:00Z',
        proposal: null,
      },
    ],
  },
]

// ---------------------------------------------------------------------- //
// Dashboard header stats + Account page balances, per account
// (PRD.md §8.1, §8.6)
// ---------------------------------------------------------------------- //

export const ACCOUNT_SNAPSHOTS: Record<AccountMode, AccountSnapshot> = {
  paper: {
    portfolioHistory: PAPER_HISTORY,
    volume24h: 18_420.55,
    balanceTrend: { changePct: 2.4, comparedTo: 'vs last 24h' },
    volumeTrend: { changePct: 15.2, comparedTo: 'vs last 24h' },
    positions: PAPER_POSITIONS,
    activity: PAPER_ACTIVITY,
    workingOrders: PAPER_WORKING_ORDERS,
    // Margin account: buying power is 2× cash, but options buying power is
    // not, because options can't be bought on margin.
    cash: 12_480.32,
    buyingPower: 24_960.64,
    optionsBuyingPower: 12_480.32,
  },
  cash: {
    portfolioHistory: CASH_HISTORY,
    volume24h: 4_860.2,
    balanceTrend: { changePct: -0.8, comparedTo: 'vs last 24h' },
    volumeTrend: { changePct: 6.3, comparedTo: 'vs last 24h' },
    positions: CASH_POSITIONS,
    activity: CASH_ACTIVITY,
    workingOrders: CASH_WORKING_ORDERS,
    // No margin here, so buying power is *below* total cash rather than
    // twice it — $240 of this balance is money you can see and cannot spend.
    // Why, exactly, is the broker's business: Alpaca reports the figure and
    // not its composition.
    cash: 3_180.45,
    buyingPower: 2_940.45,
    optionsBuyingPower: 2_940.45,
  },
}

// ---------------------------------------------------------------------- //
// Risk limits + audit log (Settings page)
// ---------------------------------------------------------------------- //

export const RISK_LIMITS: RiskLimit[] = [
  {
    key: 'max_risk_per_trade_pct',
    label: 'Max risk per trade',
    value: 7,
    unit: '%',
    min: 1,
    max: 25,
    help: 'Ceiling on what one position may lose, as a share of account equity.',
  },
  {
    key: 'max_daily_loss_pct',
    label: 'Max daily loss',
    value: 20,
    unit: '%',
    min: 1,
    max: 50,
    help: 'Realized + unrealized loss in one session that triggers an automatic halt.',
  },
  {
    key: 'max_concurrent_positions',
    label: 'Max concurrent positions',
    value: 8,
    unit: 'count',
    min: 1,
    max: 20,
    help: 'How many positions may be open at once, across every strategy.',
  },
  {
    key: 'max_exposure_per_underlying',
    label: 'Max exposure per underlying',
    value: 25,
    unit: '%',
    min: 5,
    max: 100,
    help: 'Ceiling on combined risk across every position sharing one underlying.',
  },
  {
    key: 'max_net_directional_pct',
    label: 'Max net directional exposure',
    value: 40,
    unit: '%',
    min: 5,
    max: 100,
    help: 'Ceiling on net long-minus-short delta exposure, as a share of equity.',
  },
]

export const AUDIT_LOG: AuditLogEntry[] = [
  { id: 'audit-1', time: '2026-07-15T14:00:00Z', category: 'risk', field: 'max_risk_per_trade_pct', previousValue: '5', newValue: '7' },
  { id: 'audit-2', time: '2026-06-02T09:30:00Z', category: 'risk', field: 'max_concurrent_positions', previousValue: '6', newValue: '8' },
  { id: 'audit-3', time: '2026-05-18T11:05:00Z', category: 'feed', field: 'stockHistorical', previousValue: 'iex', newValue: 'sip' },
]

// ---------------------------------------------------------------------- //
// Notifications + data sources + sentiment accuracy (Settings page)
// ---------------------------------------------------------------------- //

export const NOTIFICATION_ROUTES: NotificationRoute[] = [
  { event: 'order_filled', bell: true, discord: true },
  { event: 'order_rejected', bell: true, discord: true },
  { event: 'stop_loss_hit', bell: true, discord: true },
  { event: 'daily_loss_halt', bell: true, discord: true },
  { event: 'engine_error', bell: true, discord: true },
  { event: 'price_alert', bell: true, discord: true },
  { event: 'recommendations_ready', bell: true, discord: false },
  { event: 'strategy_promotion', bell: true, discord: false },
]

/** Seeded bell feed — one of every event type in PRD.md §10, so every
 * severity and both read states are reachable on screen. `store.tick()`
 * appends to this live as fills and stops actually occur, so the panel is
 * not merely a fixture being displayed.
 *
 * Newest first, matching the news feed and the activity ledger. */
export const NOTIFICATIONS: Notification[] = [
  { id: 'notif-1', time: '2026-08-07T14:42:00Z', event: 'order_rejected', detail: 'NVDA 220C ×4 rejected — max risk per trade (7%) would be exceeded at 9.2%.', read: false, account: 'paper' },
  { id: 'notif-2', time: '2026-08-07T14:31:00Z', event: 'stop_loss_hit', detail: 'NVDA260821C00220000 ×2 closed at $6.10 — stop loss. −$412.00.', read: false, account: 'paper' },
  { id: 'notif-3', time: '2026-08-07T14:04:00Z', event: 'order_filled', detail: 'AAPL260821C00195000 ×2 bought to open at $4.10.', read: false, account: 'paper' },
  { id: 'notif-4', time: '2026-08-07T13:58:00Z', event: 'engine_error', detail: 'Alpaca stream disconnected for 94s — engine halted itself and will not auto-resume.', read: true, account: null },
  { id: 'notif-5', time: '2026-08-07T13:30:00Z', event: 'price_alert', detail: 'MSFT is within 1% of the entry on a recommended debit spread.', read: true, account: 'paper' },
  { id: 'notif-6', time: '2026-08-07T12:15:00Z', event: 'recommendations_ready', detail: '6 candidates cleared the scanner for today’s session.', read: true, account: null },
  { id: 'notif-7', time: '2026-08-06T20:05:00Z', event: 'strategy_promotion', detail: 'Momentum Call Debit Spread has 40 paper trades at a 68% win rate — eligible for promotion.', read: true, account: null },
  // Unread, and in the *cash* book on purpose: without it the badge is
  // unreachable in one of the two accounts, and "the bell shows a count"
  // would be a state only ever seen in Paper.
  { id: 'notif-8', time: '2026-08-06T18:40:00Z', event: 'daily_loss_halt', detail: 'Session loss reached 20% of equity — new entries halted. Managed exits still running.', read: false, account: 'cash' },
]

export const DATA_SOURCES: DataSourceStatus[] = [
  { name: 'Alpaca (execution + data)', status: 'connected', detail: 'Paper account, Basic plan' },
  { name: 'Finnhub (news + calendar)', status: 'connected', detail: 'Rate limit: 42/60 req/min' },
  { name: 'FRED (macro)', status: 'connected', detail: 'Daily refresh' },
  { name: 'StockTwits (social)', status: 'degraded', detail: 'Rate limited — retrying' },
]

// ---------------------------------------------------------------------- //
// Feed selection + plan (Settings page, CLAUDE.md "Alpaca specifics")
// ---------------------------------------------------------------------- //

export const CURRENT_PLAN: DataPlan = 'basic'

export const DATA_FEEDS: DataFeed[] = [
  {
    key: 'options',
    envVar: 'ALPACA_OPTIONS_FEED',
    label: 'Options quotes',
    value: 'indicative',
    help: 'Indicative is a 15-minute-delayed derivative of OPRA, not OPRA itself.',
  },
  {
    key: 'stockHistorical',
    envVar: 'ALPACA_STOCK_FEED_HISTORICAL',
    label: 'Equity bars (historical)',
    value: 'sip',
    help: 'SIP is 100% of US volume and is free for anything older than 15 minutes.',
  },
  {
    key: 'stockRealtime',
    envVar: 'ALPACA_STOCK_FEED_REALTIME',
    label: 'Equity quotes (real-time)',
    value: 'iex',
    help: 'Real-time SIP requires the paid plan; IEX is ~2.5% of US volume.',
  },
]

/** Per-source accuracy against realized forward return (PRD.md §9).
 *
 * StockTwits sits below the 52% floor at 1h and above it at 1d, on purpose:
 * it makes the demotion state reachable on screen, and it pins which
 * reading of "accuracy falls below 52%" this app takes — *either* window
 * failing demotes the source, because a signal that is coin-flip at one
 * hour is not one to hand the scanner on the strength of its one-day
 * number. It is also already `degraded` in DATA_SOURCES, so the two panels
 * tell the same story about the same provider. */
export const SENTIMENT_ACCURACY: SentimentAccuracy[] = [
  { source: 'Finnhub', tier: 'Provider-supplied', accuracy1h: 57, accuracy1d: 61 },
  { source: 'Alpaca', tier: 'Provider-supplied', accuracy1h: 55, accuracy1d: 58 },
  { source: 'Rules', tier: 'Deterministic patterns', accuracy1h: 63, accuracy1d: 66 },
  { source: 'LLM', tier: 'Tier 3', accuracy1h: 60, accuracy1d: 64 },
  { source: 'StockTwits', tier: 'Provider-supplied', accuracy1h: 49, accuracy1d: 53 },
]

// ---------------------------------------------------------------------- //
// API key presence (Settings page, CLAUDE.md rule 6)
// ---------------------------------------------------------------------- //

export const API_KEYS: ApiKeyPresence[] = [
  { envVar: 'ALPACA_PAPER_API_KEY', purpose: 'Paper execution + market data', present: true, optional: false },
  { envVar: 'ALPACA_PAPER_SECRET_KEY', purpose: 'Paper execution + market data', present: true, optional: false },
  { envVar: 'ALPACA_LIVE_API_KEY', purpose: 'Cash execution — blank until Phase 7', present: false, optional: true },
  { envVar: 'ALPACA_LIVE_SECRET_KEY', purpose: 'Cash execution — blank until Phase 7', present: false, optional: true },
  { envVar: 'ANTHROPIC_API_KEY', purpose: 'LLM enrichment + Research chat', present: true, optional: false },
  { envVar: 'FINNHUB_API_KEY', purpose: 'News, sentiment, earnings calendar', present: true, optional: false },
  { envVar: 'FRED_API_KEY', purpose: 'Macro series', present: true, optional: false },
  { envVar: 'DISCORD_WEBHOOK_URL', purpose: 'Discord notification channel', present: true, optional: true },
]
