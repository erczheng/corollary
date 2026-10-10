import { describe, it, expect } from 'vitest'
import {
  DEFAULT_NEWS_FILTER,
  LOOKBACKS,
  SENTIMENT_BAND_TONE,
  attentionVelocity,
  compositeScore,
  consensusNet,
  etDate,
  filterNews,
  labeledShare,
  newsPublishers,
  newsSectors,
  sentimentBand,
  sentimentTone,
  sortAttention,
  sortConsensus,
  sortNews,
  orderSectors,
} from './news'
import {
  NEWS_INCOMING,
  NEWS_ITEMS,
  SECTOR_CONSENSUS,
  SENTIMENT_COMPONENTS,
  SOCIAL_ATTENTION,
} from './mockData'
import { MACRO_SECTOR, type NewsItem } from './types'

/** The feed's own clock. Every lookback and the calendar are measured
 * against this, never the wall clock — see the note in `News.tsx`. */
const NOW = NEWS_ITEMS[0].time

describe('compositeScore', () => {
  it('is the equally-weighted mean of its components', () => {
    expect(compositeScore([{ name: 'a', description: '', score: 40 }])).toBe(40)
    expect(
      compositeScore([
        { name: 'a', description: '', score: 40 },
        { name: 'b', description: '', score: 60 },
      ]),
    ).toBe(50)
  })

  /** The reason the composite is derived rather than stored: a headline
   * number that can drift from the seven rows printed underneath it
   * discredits both at once. */
  it('agrees with the breakdown the page renders beneath it', () => {
    const mean =
      SENTIMENT_COMPONENTS.reduce((sum, c) => sum + c.score, 0) / SENTIMENT_COMPONENTS.length
    expect(compositeScore(SENTIMENT_COMPONENTS)).toBe(Math.round(mean))
  })

  it('does not divide by zero on an empty set', () => {
    expect(compositeScore([])).toBe(0)
  })
})

describe('sentimentBand', () => {
  it('maps CNN’s five bands, boundaries included', () => {
    expect(sentimentBand(0)).toBe('extreme-fear')
    expect(sentimentBand(24)).toBe('extreme-fear')
    expect(sentimentBand(25)).toBe('fear')
    expect(sentimentBand(44)).toBe('fear')
    expect(sentimentBand(45)).toBe('neutral')
    expect(sentimentBand(55)).toBe('neutral')
    expect(sentimentBand(56)).toBe('greed')
    expect(sentimentBand(75)).toBe('greed')
    expect(sentimentBand(76)).toBe('extreme-greed')
    expect(sentimentBand(100)).toBe('extreme-greed')
  })

  /** Fear is the bearish end and greed the bullish one. Inverted, the gauge
   * would render a panicking market green. */
  it('runs fear bearish and greed bullish, never error', () => {
    expect(sentimentTone(10)).toBe('bearish')
    expect(sentimentTone(50)).toBe('neutral')
    expect(sentimentTone(90)).toBe('bullish')
    expect(Object.values(SENTIMENT_BAND_TONE)).not.toContain('error')
  })
})

describe('filterNews', () => {
  it('returns everything under the default filter', () => {
    expect(filterNews(NEWS_ITEMS, DEFAULT_NEWS_FILTER, NOW)).toHaveLength(NEWS_ITEMS.length)
  })

  it('narrows by sector, publisher and sentiment independently', () => {
    const sector = NEWS_ITEMS[0].sector
    expect(
      filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, sector }, NOW).every(
        (i) => i.sector === sector,
      ),
    ).toBe(true)

    const publisher = NEWS_ITEMS[0].publisher
    expect(
      filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, publisher }, NOW).every(
        (i) => i.publisher === publisher,
      ),
    ).toBe(true)

    expect(
      filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, sentiment: 'unclassified' }, NOW).every(
        (i) => i.sentiment === 'unclassified',
      ),
    ).toBe(true)
  })

  /** The filters combine rather than replacing each other — "bearish
   * Technology headlines from Reuters" is the question a hundred-row feed
   * raises, and any one control alone cannot answer it. */
  it('combines filters rather than replacing them', () => {
    const both = filterNews(
      NEWS_ITEMS,
      { ...DEFAULT_NEWS_FILTER, sector: 'Technology', sentiment: 'bearish' },
      NOW,
    )
    expect(both.length).toBeGreaterThan(0)
    expect(both.every((i) => i.sector === 'Technology' && i.sentiment === 'bearish')).toBe(true)

    const sectorOnly = filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, sector: 'Technology' }, NOW)
    expect(both.length).toBeLessThan(sectorOnly.length)
  })

  it('narrows monotonically as the lookback shortens', () => {
    const counts = LOOKBACKS.map(
      (lookback) => filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, lookback }, NOW).length,
    )
    // today <= 3d <= 1w <= 2w <= all, and the ends are genuinely different
    // — a lookback control where every step returns the same rows is not a
    // control.
    for (let i = 1; i < counts.length; i += 1) {
      expect(counts[i]).toBeGreaterThanOrEqual(counts[i - 1])
    }
    expect(counts[0]).toBeGreaterThan(0)
    expect(counts[0]).toBeLessThan(counts[counts.length - 1])
  })

  /** "Today" is today's *session*, not the trailing 24 hours. Measured as a
   * rolling window, a headline at 9:05am would drag in most of yesterday. */
  it('scopes "today" to the session rather than the last 24 hours', () => {
    const today = etDate(NOW)
    const rows = filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, lookback: 'today' }, NOW)
    expect(rows.length).toBeGreaterThan(0)
    expect(rows.every((i) => etDate(i.time) === today)).toBe(true)
  })

  it('does not mutate the feed it filters', () => {
    const before = [...NEWS_ITEMS]
    filterNews(NEWS_ITEMS, { ...DEFAULT_NEWS_FILTER, sector: 'Technology' }, NOW)
    expect(NEWS_ITEMS).toEqual(before)
  })
})

describe('sortNews', () => {
  it('sorts both directions without mutating', () => {
    const before = [...NEWS_ITEMS]
    const newest = sortNews(NEWS_ITEMS, 'newest')
    const oldest = sortNews(NEWS_ITEMS, 'oldest')

    expect(newest[0].time >= newest[newest.length - 1].time).toBe(true)
    expect(oldest[0].time <= oldest[oldest.length - 1].time).toBe(true)
    expect(oldest[0]).toEqual(newest[newest.length - 1])
    expect(NEWS_ITEMS).toEqual(before)
  })
})

describe('newsSectors', () => {
  it('offers only sectors the feed actually contains', () => {
    const sectors = newsSectors(NEWS_ITEMS)
    const present = new Set(NEWS_ITEMS.map((i) => i.sector))
    expect(sectors.every((s) => present.has(s))).toBe(true)
    expect(sectors).toHaveLength(present.size)
  })

  /** Macro is not a GICS sector — it is the bucket for stories about no
   * single name, and alphabetising it between Health Care and Technology
   * implies a peerage it does not have. */
  it('sorts macro last and the rest alphabetically', () => {
    const sectors = newsSectors(NEWS_ITEMS)
    expect(sectors[sectors.length - 1]).toBe(MACRO_SECTOR)

    const gics = sectors.slice(0, -1)
    expect(gics).toEqual([...gics].sort((a, b) => a.localeCompare(b)))
  })

  it('omits macro entirely when no macro story is present', () => {
    const companyOnly = NEWS_ITEMS.filter((i) => i.sector !== MACRO_SECTOR)
    expect(newsSectors(companyOnly)).not.toContain(MACRO_SECTOR)
  })
})

describe('newsPublishers', () => {
  it('lists each publisher once, sorted', () => {
    const publishers = newsPublishers(NEWS_ITEMS)
    expect(publishers).toEqual([...new Set(publishers)])
    expect(publishers).toEqual([...publishers].sort((a, b) => a.localeCompare(b)))
    expect(publishers.length).toBeGreaterThan(1)
  })
})

describe('the news corpus', () => {
  it('covers every sentiment, including the ugly one', () => {
    const seen = new Set(NEWS_ITEMS.map((i) => i.sentiment))
    expect(seen).toEqual(new Set(['bullish', 'bearish', 'neutral', 'unclassified']))
  })

  it('covers all three classification tiers', () => {
    const seen = new Set(NEWS_ITEMS.map((i) => i.tier))
    expect(seen).toEqual(new Set(['provider', 'rules', 'llm']))
  })

  /** PRD.md §9: tiers 1 and 2 always publish a direction, so an unlabelled
   * item is always tier 3 falling below its confidence threshold. If a
   * `provider` or `rules` item ever reads Unclassified, either the fixture
   * or the story that produced it is wrong. */
  it('only ever leaves an LLM-tier item unclassified', () => {
    const wrong = [...NEWS_ITEMS, ...NEWS_INCOMING].filter(
      (i) => i.sentiment === 'unclassified' && i.tier !== 'llm',
    )
    expect(wrong).toEqual([])
  })

  it('files every MARKET story under the macro sector, and nothing else', () => {
    for (const item of NEWS_ITEMS) {
      expect(item.ticker === 'MARKET').toBe(item.sector === MACRO_SECTOR)
    }
  })

  it('has no weekend headlines', () => {
    for (const item of NEWS_ITEMS) {
      const day = new Date(item.time).getUTCDay()
      expect(day).not.toBe(0)
      expect(day).not.toBe(6)
    }
  })

  it('is deep enough to page and unique by id', () => {
    expect(NEWS_ITEMS.length).toBeGreaterThan(45)
    expect(new Set(NEWS_ITEMS.map((i) => i.id)).size).toBe(NEWS_ITEMS.length)
  })

  it('is newest first as published', () => {
    for (let i = 1; i < NEWS_ITEMS.length; i += 1) {
      expect(NEWS_ITEMS[i - 1].time >= NEWS_ITEMS[i].time).toBe(true)
    }
  })
})

describe('attentionVelocity', () => {
  /** Velocity, not raw mentions: a name going from 260 to 780 is the
   * signal, while SPY's 1,650 is just SPY. */
  it('measures a name against its own baseline, not the field', () => {
    const spy = SOCIAL_ATTENTION.find((i) => i.ticker === 'SPY')!
    const alab = SOCIAL_ATTENTION.find((i) => i.ticker === 'ALAB')!

    expect(spy.mentions).toBeGreaterThan(alab.mentions)
    expect(attentionVelocity(alab)).toBeGreaterThan(attentionVelocity(spy))
  })

  it('reads below 1 for a name under its baseline', () => {
    const quiet = SOCIAL_ATTENTION.filter((i) => attentionVelocity(i) < 1)
    expect(quiet.length).toBeGreaterThan(0)
  })

  it('does not divide by zero on an absent baseline', () => {
    expect(
      attentionVelocity({
        ticker: 'X',
        mentions: 10,
        baselineMentions: 0,
        labeledCount: 0,
        sampleSize: 0,
        sentiment: 'neutral',
      }),
    ).toBe(0)
  })

  it('sorts most unusual first, without mutating', () => {
    const before = [...SOCIAL_ATTENTION]
    const sorted = sortAttention(SOCIAL_ATTENTION)
    for (let i = 1; i < sorted.length; i += 1) {
      expect(attentionVelocity(sorted[i - 1])).toBeGreaterThanOrEqual(attentionVelocity(sorted[i]))
    }
    expect(SOCIAL_ATTENTION).toEqual(before)
  })
})

describe('labeledShare', () => {
  it('is the labelled count over the sample', () => {
    expect(
      labeledShare({
        ticker: 'X',
        mentions: 100,
        baselineMentions: 50,
        labeledCount: 40,
        sampleSize: 100,
        sentiment: 'bullish',
      }),
    ).toBeCloseTo(0.4)
  })

  it('does not divide by zero on an empty sample', () => {
    expect(
      labeledShare({
        ticker: 'X',
        mentions: 0,
        baselineMentions: 50,
        labeledCount: 0,
        sampleSize: 0,
        sentiment: 'neutral',
      }),
    ).toBe(0)
  })

  /** Sentiment is aggregated over labelled messages only (PRD.md §8.3), so
   * the fixture has to reach the thin-coverage case the column exists for. */
  it('has a fixture with visibly thin coverage', () => {
    const thin = SOCIAL_ATTENTION.filter((i) => labeledShare(i) < 0.2)
    expect(thin.length).toBeGreaterThan(0)
    expect(thin.every((i) => labeledShare(i) > 0)).toBe(true)
  })
})

describe('sector consensus', () => {
  it('has each row summing to 100', () => {
    for (const c of SECTOR_CONSENSUS) {
      expect(c.buy + c.hold + c.sell).toBe(100)
    }
  })

  /** Buy minus sell, not buy alone — 55/38/7 above 52/40/8 ignores that the
   * second has barely more sells. */
  it('ranks on the net, not the buy share', () => {
    const sorted = sortConsensus(SECTOR_CONSENSUS)
    for (let i = 1; i < sorted.length; i += 1) {
      expect(consensusNet(sorted[i - 1])).toBeGreaterThanOrEqual(consensusNet(sorted[i]))
    }
  })

  it('covers a sector the street is net-cautious on', () => {
    expect(SECTOR_CONSENSUS.some((c) => c.buy < 40)).toBe(true)
  })

  it('does not sort in place', () => {
    const before = [...SECTOR_CONSENSUS]
    sortConsensus(SECTOR_CONSENSUS)
    expect(SECTOR_CONSENSUS).toEqual(before)
  })
})

describe('etDate', () => {
  /** The whole reason the calendar stores its own date: midnight UTC is the
   * previous evening in New York. */
  it('reports the Eastern date, not the UTC one', () => {
    expect(etDate('2026-08-12T00:00:00Z')).toBe('2026-08-11')
    expect(etDate('2026-08-12T13:00:00Z')).toBe('2026-08-12')
  })
})

describe('the incoming reserve', () => {
  it('is a distinct set of items from the published corpus', () => {
    const published = new Set(NEWS_ITEMS.map((i: NewsItem) => i.id))
    expect(NEWS_INCOMING.every((i) => !published.has(i.id))).toBe(true)
    expect(NEWS_INCOMING.length).toBeGreaterThan(0)
  })
})

describe('the live feed helpers (Phase 3 step 4)', () => {
  /** A null publisher is "the vendor named none" — not a value to filter on,
   * and never the string "null" in a dropdown. */
  it('leaves a missing publisher out of the publisher options', () => {
    const base = NEWS_ITEMS[0]
    const items: NewsItem[] = [
      { ...base, id: 'a', publisher: 'Reuters' },
      { ...base, id: 'b', publisher: null },
      { ...base, id: 'c', publisher: 'Benzinga' },
    ]
    expect(newsPublishers(items)).toEqual(['Benzinga', 'Reuters'])
  })

  it('orders seen sectors alphabetically with Macro last, once each', () => {
    expect(orderSectors(['Other', MACRO_SECTOR, 'Energy', 'Other'])).toEqual([
      'Energy',
      'Other',
      MACRO_SECTOR,
    ])
  })
})
