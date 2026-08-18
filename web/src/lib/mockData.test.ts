import { describe, it, expect } from 'vitest'
import {
  ACCOUNT_SNAPSHOTS,
  ACTIVITY_STATUS_LABEL,
  BENCHMARK_HISTORY,
  CHAIN_EXPIRATIONS,
  MARKET_TODAY,
  OPTION_CHAIN,
  STOCKS,
  STRATEGIES,
  UNDERLYINGS,
  activityStats,
  type ActivityStatus,
} from './mockData'

/** The fixtures are generated from a seeded PRNG so screenshots and tests
 * don't flake between runs (CLAUDE.md, mockData.ts). "Seeded" only holds if
 * something notices when the draw order changes — inserting a series that
 * shares the module-level stream silently shifts every series after it. */
describe('generated series are deterministic', () => {
  it('pins the paper account to its known values', () => {
    const h = ACCOUNT_SNAPSHOTS.paper.portfolioHistory
    expect(h).toHaveLength(261)
    expect(h[0]).toEqual({ date: '2025-08-08', value: 24_969.91 })
    expect(h[h.length - 1]).toEqual({ date: '2026-08-07', value: 32_899.85 })
  })

  it('pins the cash account to its known values', () => {
    const h = ACCOUNT_SNAPSHOTS.cash.portfolioHistory
    expect(h).toHaveLength(261)
    expect(h[0]).toEqual({ date: '2025-08-08', value: 8_012.3 })
    expect(h[h.length - 1]).toEqual({ date: '2026-08-07', value: 9_712.41 })
  })

  it('runs the benchmark over the same dates as the portfolio', () => {
    const paper = ACCOUNT_SNAPSHOTS.paper.portfolioHistory
    expect(BENCHMARK_HISTORY).toHaveLength(paper.length)
    expect(BENCHMARK_HISTORY[0].date).toBe(paper[0].date)
  })

  it('never emits a weekend close', () => {
    for (const p of ACCOUNT_SNAPSHOTS.cash.portfolioHistory) {
      const day = new Date(`${p.date}T00:00:00Z`).getUTCDay()
      expect(day).not.toBe(0)
      expect(day).not.toBe(6)
    }
  })
})

describe('paper and cash are genuinely different accounts', () => {
  it('holds different money in each', () => {
    const paper = ACCOUNT_SNAPSHOTS.paper
    const cash = ACCOUNT_SNAPSHOTS.cash
    const last = (h: { value: number }[]) => h[h.length - 1].value

    expect(last(cash.portfolioHistory)).not.toBe(last(paper.portfolioHistory))
    expect(cash.volume24h).not.toBe(paper.volume24h)
  })

  it('covers a falling balance as well as a rising one', () => {
    // A trend card that has only ever been rendered green is a state nobody
    // has actually looked at.
    expect(ACCOUNT_SNAPSHOTS.paper.balanceTrend.changePct).toBeGreaterThan(0)
    expect(ACCOUNT_SNAPSHOTS.cash.balanceTrend.changePct).toBeLessThan(0)
  })
})

describe('fixtures cover the states the Dashboard can render', () => {
  it('includes a strategy that has never traded live', () => {
    expect(STRATEGIES.some((s) => s.live === null)).toBe(true)
  })

  it('gives both accounts a long and a short, so Close and Flatten have both paths', () => {
    for (const mode of ['paper', 'cash'] as const) {
      const positions = ACCOUNT_SNAPSHOTS[mode].positions
      expect(positions.some((p) => p.direction === 'long')).toBe(true)
      expect(positions.some((p) => p.direction === 'short')).toBe(true)
    }
  })
})

/** CLAUDE.md: "Cover every state a component can render, including the ugly
 * ones — a tier or empty state absent from the fixtures is one nobody can
 * see." `pending` was exactly that until the Activity page existed:
 * ACTIVITY_STATUS_CLASS had a caution branch no fixture ever reached. */
describe('the activity feed reaches every state the table renders', () => {
  const statuses = Object.keys(ACTIVITY_STATUS_LABEL) as ActivityStatus[]

  it('renders all four statuses somewhere in the paper feed', () => {
    const present = new Set(ACCOUNT_SNAPSHOTS.paper.activity.map((a) => a.status))
    for (const status of statuses) {
      expect(present.has(status)).toBe(true)
    }
  })

  it('carries a deposit and a withdrawal, so the cash-movement branch renders', () => {
    const actions = new Set(ACCOUNT_SNAPSHOTS.paper.activity.map((a) => a.action))
    expect(actions.has('DEPOSIT')).toBe(true)
    expect(actions.has('WITHDRAWAL')).toBe(true)
  })

  it('gives every rejection a reason — a silent rejection is a bug', () => {
    for (const mode of ['paper', 'cash'] as const) {
      for (const item of ACCOUNT_SNAPSHOTS[mode].activity) {
        if (item.status === 'rejected') {
          expect(item.rejectionReason).toBeTruthy()
        }
      }
    }
  })

  it('is deep enough that the Activity page has more than one page', () => {
    // 15 rows per page. If the fixture ever shrinks below this, Pagination
    // silently stops rendering and nobody notices it regressed.
    expect(ACCOUNT_SNAPSHOTS.paper.activity.length).toBeGreaterThan(15)
    expect(ACCOUNT_SNAPSHOTS.cash.activity.length).toBeGreaterThan(15)
  })

  it('only ever attaches P&L to a filled closing trade', () => {
    for (const mode of ['paper', 'cash'] as const) {
      for (const item of ACCOUNT_SNAPSHOTS[mode].activity) {
        if (item.pnl !== null) {
          expect(item.status).toBe('filled')
          expect(['STC', 'BTC']).toContain(item.action)
        }
        // The dollar figure and the percentage are present or absent
        // together — one without the other is an unrenderable half-state.
        expect(item.pnl === null).toBe(item.pnlPct === null)
      }
    }
  })

  it('never emits a weekend fill', () => {
    for (const item of ACCOUNT_SNAPSHOTS.paper.activity) {
      const day = new Date(item.time).getUTCDay()
      expect(day).not.toBe(0)
      expect(day).not.toBe(6)
    }
  })
})

/** The activity tail is generated, so "seeded" only holds if something
 * notices when the draw changes. These are the same guardrails the price
 * series has above — a silent shift here would move every number in the
 * Activity header without any test failing. */
describe('the generated activity feed is pinned', () => {
  it('holds the paper feed to its known shape', () => {
    const feed = ACCOUNT_SNAPSHOTS.paper.activity
    const stats = activityStats(feed)

    expect(feed).toHaveLength(53)
    expect(stats.wins).toBe(10)
    expect(stats.losses).toBe(8)
    expect(stats.lifetimePnl).toBe(533.04)
    expect(stats.avgWin).toBeCloseTo(141.0, 2)
    expect(stats.avgLoss).toBeCloseTo(-109.62, 2)
  })

  it('holds the cash feed to its known shape', () => {
    const feed = ACCOUNT_SNAPSHOTS.cash.activity
    const stats = activityStats(feed)

    expect(feed).toHaveLength(24)
    expect(stats.lifetimePnl).toBe(68.47)
  })

  /** The feed has to agree with what the rest of the app says about this
   * account. STRATEGIES.live puts strat-1 at a 71% win rate and a 1.5
   * profit factor; an Activity page showing a break-even 50% account would
   * have two screens contradicting each other about the same money. */
  it('agrees with the profitability the strategy stats claim', () => {
    for (const mode of ['paper', 'cash'] as const) {
      const stats = activityStats(ACCOUNT_SNAPSHOTS[mode].activity)
      const profitFactor = (stats.wins * stats.avgWin!) / (stats.losses * Math.abs(stats.avgLoss!))

      expect(stats.lifetimePnl).toBeGreaterThan(0)
      expect(profitFactor).toBeGreaterThan(1.2)
    }
  })
})

describe('activityStats', () => {
  it('averages wins and losses separately and sums only realized P&L', () => {
    const stats = activityStats([
      { id: 'a', time: '2026-08-07T14:00:00Z', contract: 'X', action: 'STC', price: 1, quantity: 1, pnl: 100, pnlPct: 20, amount: null, status: 'filled' },
      { id: 'b', time: '2026-08-07T14:01:00Z', contract: 'X', action: 'STC', price: 1, quantity: 1, pnl: 300, pnlPct: 40, amount: null, status: 'filled' },
      { id: 'c', time: '2026-08-07T14:02:00Z', contract: 'X', action: 'BTC', price: 1, quantity: 1, pnl: -50, pnlPct: -10, amount: null, status: 'filled' },
      // Neither of these contributes: an opening fill has realized nothing,
      // and a deposit is money moved rather than money made.
      { id: 'd', time: '2026-08-07T14:03:00Z', contract: 'X', action: 'BTO', price: 1, quantity: 1, pnl: null, pnlPct: null, amount: null, status: 'filled' },
      { id: 'e', time: '2026-08-07T14:04:00Z', contract: '—', action: 'DEPOSIT', price: null, quantity: null, pnl: null, pnlPct: null, amount: 10_000, status: 'filled' },
    ])

    expect(stats.avgWin).toBe(200)
    expect(stats.avgWinPct).toBe(30)
    expect(stats.avgLoss).toBe(-50)
    expect(stats.avgLossPct).toBe(-10)
    expect(stats.wins).toBe(2)
    expect(stats.losses).toBe(1)
    // 100 + 300 - 50. The 10,000 deposit is not performance.
    expect(stats.lifetimePnl).toBe(350)
  })

  it('reports an unknown average as null, never as zero', () => {
    const stats = activityStats([
      { id: 'a', time: '2026-08-07T14:00:00Z', contract: 'X', action: 'STC', price: 1, quantity: 1, pnl: 40, pnlPct: 8, amount: null, status: 'filled' },
    ])

    // No losing trade yet. An average loss of $0.00 would claim the account
    // has never lost money on a trade, which is a different statement.
    expect(stats.avgLoss).toBeNull()
    expect(stats.avgLossPct).toBeNull()
    expect(stats.losses).toBe(0)
  })
})

/** The Markets chain is generated, so "seeded" only holds if something
 * notices when the draw changes — the same guardrail the price series and
 * the activity feed carry above. */
describe('the option chain is pinned and internally consistent', () => {
  it('holds the chain to its known shape', () => {
    expect(OPTION_CHAIN).toHaveLength(180)

    const atm = OPTION_CHAIN.find(
      (c) => c.symbol === 'AAPL' && c.expiration === '2026-08-21' && c.strike === 230 && c.type === 'call',
    )!
    expect(atm.last).toBe(7.41)
    expect(atm.bid).toBe(7.31)
    expect(atm.ask).toBe(7.51)
    expect(atm.volume).toBe(26_056)
    expect(atm.iv).toBe(0.284)

    // Put-call parity at the same strike, which the flat pricing vol makes
    // exact: C - P is the forward less the strike. A chain that fails it
    // is one where the calls and the puts were generated independently.
    const put = OPTION_CHAIN.find(
      (c) => c.symbol === 'AAPL' && c.expiration === '2026-08-21' && c.strike === 230 && c.type === 'put',
    )!
    expect(atm.last - put.last).toBeCloseTo(UNDERLYINGS.AAPL.price - 230, 2)
  })

  it('is deep enough that the chain paginates', () => {
    // 15 rows per page, and the page opens scoped to one underlying — so
    // it is the per-symbol depth that has to clear the bar, not the total.
    for (const symbol of new Set(OPTION_CHAIN.map((c) => c.symbol))) {
      expect(OPTION_CHAIN.filter((c) => c.symbol === symbol).length).toBeGreaterThan(15)
    }
  })

  it('never quotes a last outside its own spread', () => {
    for (const c of OPTION_CHAIN) {
      expect(c.bid).toBeGreaterThan(0)
      expect(c.ask).toBeGreaterThan(c.bid)
      expect(c.last).toBeGreaterThanOrEqual(c.bid)
      expect(c.last).toBeLessThanOrEqual(c.ask)
    }
  })

  it('agrees with itself about the day: change, percent and yesterday close', () => {
    for (const c of OPTION_CHAIN) {
      const previousClose = c.last - c.change
      // A percentage measured against a non-positive close is not a
      // percentage. Every contract has to have been worth something
      // yesterday.
      expect(previousClose).toBeGreaterThan(0)
      expect(c.changePct).toBeCloseTo((c.change / previousClose) * 100, 1)
    }
  })

  it('reads like a chain — calls cheapen as strikes rise, puts richen', () => {
    // A chain that fails this is one no trader would believe, and it would
    // make the strike ladder look shuffled even when it is sorted right.
    for (const symbol of new Set(OPTION_CHAIN.map((c) => c.symbol))) {
      for (const expiration of new Set(OPTION_CHAIN.map((c) => c.expiration))) {
        for (const type of ['call', 'put'] as const) {
          const ladder = OPTION_CHAIN.filter(
            (c) => c.symbol === symbol && c.expiration === expiration && c.type === type,
          ).sort((a, b) => a.strike - b.strike)

          expect(ladder.length).toBeGreaterThan(1)
          for (let i = 1; i < ladder.length; i++) {
            if (type === 'call') expect(ladder[i].last).toBeLessThan(ladder[i - 1].last)
            else expect(ladder[i].last).toBeGreaterThan(ladder[i - 1].last)
          }
        }
      }
    }
  })

  it('moves the calls and the puts in opposite directions on the day', () => {
    // The change is derived from the underlying's move, not drawn on its
    // own. Drawn independently, both sides of a name rallied at once and
    // "top gainers" was noise rather than a read on the session.
    for (const symbol of new Set(OPTION_CHAIN.map((c) => c.symbol))) {
      const rows = OPTION_CHAIN.filter((c) => c.symbol === symbol && c.expiration === '2026-08-21')
      const calls = rows.filter((c) => c.type === 'call')
      const puts = rows.filter((c) => c.type === 'put')
      const up = UNDERLYINGS[symbol].change > 0

      expect(calls.every((c) => (up ? c.change > 0 : c.change < 0))).toBe(true)
      expect(puts.every((c) => (up ? c.change < 0 : c.change > 0))).toBe(true)
    }
  })

  it('expires on a weekday, in the future', () => {
    for (const expiration of CHAIN_EXPIRATIONS) {
      const day = new Date(`${expiration}T00:00:00Z`).getUTCDay()
      expect(day).not.toBe(0)
      expect(day).not.toBe(6)
      expect(expiration > MARKET_TODAY).toBe(true)
    }
  })
})

describe('the stock universe covers what the Markets table renders', () => {
  it('is deep enough to paginate and holds no duplicate symbol', () => {
    expect(STOCKS.length).toBeGreaterThan(15)
    expect(new Set(STOCKS.map((s) => s.symbol)).size).toBe(STOCKS.length)
  })

  it('carries a fund with no market cap and a company with one', () => {
    // The em-dash branch and the null-sorts-last rule both need a fixture
    // that reaches them.
    expect(STOCKS.some((s) => s.marketCap === null)).toBe(true)
    expect(STOCKS.some((s) => s.marketCap !== null && s.marketCap > 0)).toBe(true)
    expect(STOCKS.some((s) => s.changePct > 0)).toBe(true)
    expect(STOCKS.some((s) => s.changePct < 0)).toBe(true)
  })

  it('never prices a stock differently from the quote the rest of the app reads', () => {
    // Markets, the Activity rows and the payoff charts all name the same
    // stock. One number, one source — UNDERLYINGS.
    for (const s of STOCKS) {
      const quote = UNDERLYINGS[s.symbol]
      if (!quote) continue
      expect(s.price).toBe(quote.price)
      expect(s.change).toBe(quote.change)
      expect(s.changePct).toBe(quote.changePct)
    }
  })

  it('spans enough listing dates that "new listings" is a different table', () => {
    const listed = STOCKS.map((s) => s.listedOn).sort()
    expect(listed[listed.length - 1] > '2024-01-01').toBe(true)
    expect(listed[0] < '1990-01-01').toBe(true)
  })
})
