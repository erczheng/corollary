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

function mulberry32(seed: number) {
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

export interface PricePoint {
  date: string // ISO date
  value: number
}

/** A one-year daily series, portfolio and SPY-benchmark, both indexed to
 * the same starting value so the overlay is visually comparable. First run
 * was 2025-08-08 per Corollary's rule of seeding at t0 — no
 * pre-Corollary reconstruction. */
export const PORTFOLIO_HISTORY: PricePoint[] = (() => {
  const points: PricePoint[] = []
  let value = 25_000
  const start = new Date('2025-08-08T00:00:00Z')
  for (let i = 0; i < 365; i++) {
    const date = new Date(start)
    date.setUTCDate(date.getUTCDate() + i)
    const day = date.getUTCDay()
    if (day === 0 || day === 6) continue // market closed weekends
    const drift = 0.0006 // slight upward bias
    const noise = (rand() - 0.48) * 0.018
    value = value * (1 + drift + noise)
    points.push({ date: date.toISOString().slice(0, 10), value: Math.round(value * 100) / 100 })
  }
  return points
})()

export const BENCHMARK_HISTORY: PricePoint[] = (() => {
  const points: PricePoint[] = []
  let value = 25_000
  const start = new Date('2025-08-08T00:00:00Z')
  for (let i = 0; i < 365; i++) {
    const date = new Date(start)
    date.setUTCDate(date.getUTCDate() + i)
    const day = date.getUTCDay()
    if (day === 0 || day === 6) continue
    const drift = 0.00035
    const noise = (rand() - 0.5) * 0.01
    value = value * (1 + drift + noise)
    points.push({ date: date.toISOString().slice(0, 10), value: Math.round(value * 100) / 100 })
  }
  return points
})()

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
  contract: string
  setup: string
  confidence: number | null // null renders as "—" — no base rate yet
  unvalidated: boolean
  origin: 'scanner' | 'llm'
}

/* Deliberately spans every confidence state the UI can render — high
 * (>=65), medium (50-64), low (<50), and null — so all four are visible
 * on screen rather than only the ones that happen to occur. */
export const RECOMMENDATIONS: Recommendation[] = [
  { id: 'rec-1', symbol: 'SPY', contract: '$560/$555 Put Credit Spread, Nov 21', setup: 'mean_reversion', confidence: 71, unvalidated: false, origin: 'scanner' },
  { id: 'rec-2', symbol: 'AAPL', contract: '$235 Call, Dec 19', setup: 'earnings_drift', confidence: 64, unvalidated: false, origin: 'scanner' },
  { id: 'rec-3', symbol: 'NVDA', contract: '$145/$140 Put Credit Spread, Nov 21', setup: 'iv_crush', confidence: 58, unvalidated: false, origin: 'scanner' },
  { id: 'rec-4', symbol: 'QQQ', contract: '$495 Call, Jan 16', setup: 'unclassified', confidence: null, unvalidated: true, origin: 'llm' },
  { id: 'rec-5', symbol: 'TSLA', contract: '$260/$250 Put Credit Spread, Dec 19', setup: 'mean_reversion', confidence: 69, unvalidated: false, origin: 'scanner' },
  { id: 'rec-6', symbol: 'IWM', contract: '$205 Put, Dec 19', setup: 'gap_fade', confidence: 44, unvalidated: false, origin: 'scanner' },
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

export interface ActivityItem {
  id: string
  time: string // ISO datetime
  contract: string
  action: ActivityAction
  price: number | null
  quantity: number | null
  pnl: number | null
  status: ActivityStatus
  rejectionReason?: string
}

export const RECENT_ACTIVITY: ActivityItem[] = [
  { id: 'act-1', time: '2026-08-07T14:32:00Z', contract: 'AAPL $230 Call Aug 15', action: 'STC', price: 4.85, quantity: 2, pnl: 62.0, status: 'filled' },
  { id: 'act-2', time: '2026-08-07T13:05:00Z', contract: 'TSLA $240 Put Nov 15', action: 'BTO', price: 4.1, quantity: 1, pnl: null, status: 'filled' },
  { id: 'act-3', time: '2026-08-07T10:48:00Z', contract: 'SPY $430/$425 Put Credit Spread Oct 17', action: 'STO', price: 0.7, quantity: 3, pnl: null, status: 'filled' },
  { id: 'act-4', time: '2026-08-06T19:58:00Z', contract: 'QQQ $370 Call Dec 20', action: 'BTO', price: 6.4, quantity: 1, pnl: null, status: 'rejected', rejectionReason: 'max_exposure_per_underlying: QQQ already at 27% of equity (limit 25%)' },
  { id: 'act-5', time: '2026-08-06T15:41:00Z', contract: 'NVDA $150 Put Nov 21', action: 'STC', price: 2.15, quantity: 2, pnl: -102.5, status: 'filled' },
  { id: 'act-6', time: '2026-08-06T09:31:00Z', contract: '—', action: 'DEPOSIT', price: null, quantity: null, pnl: null, status: 'filled' },
  { id: 'act-7', time: '2026-08-05T16:02:00Z', contract: 'AMZN $185 Call Sep 19', action: 'BTO', price: 3.2, quantity: 4, pnl: null, status: 'canceled' },
]

// ---------------------------------------------------------------------- //
// Open positions (Activity page)
// ---------------------------------------------------------------------- //

export interface Position {
  id: string
  symbol: string
  contract: string
  last: number
  costBasis: number
  value: number
  quantity: number
  pnl: number
  pnlPct: number
  bid: number
  ask: number
}

export const OPEN_POSITIONS: Position[] = [
  { id: 'pos-1', symbol: 'AAPL', contract: '$230 Call Oct 17', last: 232.4, costBasis: 350.0, value: 412.0, quantity: 2, pnl: 62.0, pnlPct: 17.71, bid: 2.04, ask: 2.08 },
  { id: 'pos-2', symbol: 'TSLA', contract: '$240 Put Nov 15', last: 238.1, costBasis: 410.0, value: 307.5, quantity: 1, pnl: -102.5, pnlPct: -25.0, bid: 3.02, ask: 3.12 },
  { id: 'pos-3', symbol: 'SPY', contract: '$430/$425 Put Credit Spread Oct 17', last: 429.88, costBasis: 210.0, value: 168.0, quantity: 3, pnl: 42.0, pnlPct: 20.0, bid: 0.55, ask: 0.6 },
  { id: 'pos-4', symbol: 'QQQ', contract: '$370 Call Dec 20', last: 372.4, costBasis: 640.0, value: 640.0, quantity: 1, pnl: 0, pnlPct: 0, bid: 6.35, ask: 6.45 },
]

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

export const OPTION_CHAIN: OptionContract[] = [
  { symbol: 'AAPL', strike: 225, expiration: '2026-08-15', type: 'call', last: 8.4, change: 0.35, changePct: 4.35, bid: 8.3, ask: 8.5, volume: 4210, openInterest: 12300, iv: 0.28 },
  { symbol: 'AAPL', strike: 230, expiration: '2026-08-15', type: 'call', last: 4.85, change: -0.12, changePct: -2.41, bid: 4.8, ask: 4.9, volume: 8890, openInterest: 20100, iv: 0.27 },
  { symbol: 'AAPL', strike: 235, expiration: '2026-08-15', type: 'call', last: 2.1, change: -0.2, changePct: -8.7, bid: 2.05, ask: 2.15, volume: 6120, openInterest: 15400, iv: 0.29 },
  { symbol: 'AAPL', strike: 220, expiration: '2026-08-15', type: 'put', last: 1.9, change: -0.08, changePct: -4.04, bid: 1.85, ask: 1.95, volume: 3010, openInterest: 9800, iv: 0.3 },
  { symbol: 'AAPL', strike: 215, expiration: '2026-08-15', type: 'put', last: 0.95, change: -0.03, changePct: -3.06, bid: 0.9, ask: 1.0, volume: 2100, openInterest: 7100, iv: 0.31 },
]

export interface StockQuote {
  symbol: string
  name: string
  price: number
  change: number
  changePct: number
  volume: number
  marketCap: number // in billions
}

export const STOCKS: StockQuote[] = [
  { symbol: 'AAPL', name: 'Apple Inc.', price: 232.4, change: 3.15, changePct: 1.37, volume: 48_200_000, marketCap: 3540 },
  { symbol: 'NVDA', name: 'NVIDIA Corp.', price: 138.2, change: -2.1, changePct: -1.5, volume: 210_500_000, marketCap: 3390 },
  { symbol: 'TSLA', name: 'Tesla Inc.', price: 238.1, change: -6.4, changePct: -2.62, volume: 92_100_000, marketCap: 760 },
  { symbol: 'SPY', name: 'SPDR S&P 500 ETF', price: 429.88, change: 1.2, changePct: 0.28, volume: 61_400_000, marketCap: 0 },
  { symbol: 'QQQ', name: 'Invesco QQQ Trust', price: 372.4, change: 2.05, changePct: 0.55, volume: 33_700_000, marketCap: 0 },
]

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
// Dashboard header stats
// ---------------------------------------------------------------------- //

export const VOLUME_24H = 18_420.55

export const DASHBOARD_TRENDS = {
  balance: { changePct: 2.4, comparedTo: 'vs last 24h' },
  volume: { changePct: 15.2, comparedTo: 'vs last 24h' },
  winRate: { changePct: -1.2, comparedTo: 'vs 30d avg' },
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
