import { describe, it, expect } from 'vitest'
import { AUDIT_LOG, DATA_FEEDS, RISK_LIMITS, SENTIMENT_AUDIT_STATES } from './mockData'
import { NOTIFICATION_EVENT_LABEL, type NotificationEvent, type RiskLimit } from './types'
import {
  ApiError,
  NETWORK_UNREACHABLE,
  SENTIMENT_AUDIT_RATE_LIMITED,
  SENTIMENT_AUDIT_RUNNING,
  SENTIMENT_AUDIT_UNAVAILABLE,
} from './api'
import { formatTenthsPct } from './format'
import {
  SENTIMENT_DEMOTE_SAMPLE,
  SENTIMENT_FLOOR,
  SENTIMENT_REPROMOTE,
  SENTIMENT_REPROMOTE_SAMPLE,
  accuracyTenths,
  actingCell,
  auditEntry,
  auditFieldLabel,
  auditTriggerRefusal,
  cellCounts,
  demotedSources,
  feedOptionsFor,
  feedWarning,
  isCriticalEvent,
  isInsufficientSample,
  isRaise,
  nothingAuditedYet,
  notificationAuditField,
  repromotionProgress,
  riskDollars,
  riskLimitFor,
  sourceStatusKind,
  validateRiskLimit,
} from './settings'

const limit = (key: string): RiskLimit => {
  const found = RISK_LIMITS.find((l) => l.key === key)
  if (!found) throw new Error(`no fixture for ${key}`)
  return found
}

/** CLAUDE.md's testing table asks for two tests per limit: one proving it
 * rejects, and one proving it *permits at the boundary*. The boundary case
 * is the one that catches an off-by-one in a comparison operator, which is
 * how a ceiling ends up one point tighter or looser than it reads. */
describe('validateRiskLimit', () => {
  it.each(RISK_LIMITS.map((l) => [l.key, l] as const))(
    'permits %s at both boundaries',
    (_key, l) => {
      expect(validateRiskLimit(l, l.min)).toBeNull()
      expect(validateRiskLimit(l, l.max)).toBeNull()
    },
  )

  it.each(RISK_LIMITS.map((l) => [l.key, l] as const))(
    'rejects %s just outside both boundaries',
    (_key, l) => {
      expect(validateRiskLimit(l, l.min - 1)).not.toBeNull()
      expect(validateRiskLimit(l, l.max + 1)).not.toBeNull()
    },
  )

  it('rejects a fractional position count but allows a fractional percentage', () => {
    expect(validateRiskLimit(limit('max_concurrent_positions'), 8.5)).not.toBeNull()
    expect(validateRiskLimit(limit('max_risk_per_trade_pct'), 7.5)).toBeNull()
  })

  it('rejects values that are not numbers at all', () => {
    expect(validateRiskLimit(limit('max_risk_per_trade_pct'), Number.NaN)).not.toBeNull()
    expect(validateRiskLimit(limit('max_risk_per_trade_pct'), Number.POSITIVE_INFINITY)).not.toBeNull()
  })

  it('rejects zero and negatives even where min would allow the arithmetic', () => {
    expect(validateRiskLimit(limit('max_risk_per_trade_pct'), 0)).not.toBeNull()
    expect(validateRiskLimit(limit('max_risk_per_trade_pct'), -7)).not.toBeNull()
  })
})

/** Raising a ceiling is the direction that puts more money at risk, so it is
 * the direction that gets a confirm. Lowering one is always safe and must
 * not nag — a control that asks twice for the cautious action trains you to
 * click through the dangerous one. */
describe('isRaise', () => {
  it('is true only when the new value exceeds the current one', () => {
    const l = limit('max_risk_per_trade_pct') // 7
    expect(isRaise(l, 10)).toBe(true)
    expect(isRaise(l, 5)).toBe(false)
    expect(isRaise(l, 7)).toBe(false)
  })

  /** `value: null` means no ceiling is configured. There is nothing to raise
   * *from*, and a confirm would have to quote a previous figure nobody set —
   * the `?? 7` mistake wearing a dialog. A bare `next > null` would coerce to
   * zero and call every first ceiling a raise. */
  it('is false against an unconfigured ceiling, whatever the new value', () => {
    const l: RiskLimit = { ...limit('max_risk_per_trade_pct'), value: null }
    expect(isRaise(l, 25)).toBe(false)
    expect(isRaise(l, 1)).toBe(false)
  })
})

describe('riskDollars', () => {
  it('converts a percentage ceiling into money against equity', () => {
    expect(riskDollars(20_620.32, 7)).toBeCloseTo(1_443.42, 2)
    expect(riskDollars(20_620.32, 10)).toBeCloseTo(2_062.03, 2)
  })

  it('is zero at zero equity rather than NaN', () => {
    expect(riskDollars(0, 7)).toBe(0)
  })
})

/** Both order tickets used to read this out of the fixture array with
 * *different* fallbacks — `?? 7` in one and `?? 0` in the other. One invents
 * a ceiling that was never configured and the other reports every trade as
 * over-limit. Returning null instead forces the caller to say "no ceiling
 * known" rather than pick a number to stand in for one. */
describe('riskLimitFor', () => {
  it('reads the value from the limits it is given, not from the fixture', () => {
    const edited = RISK_LIMITS.map((l) =>
      l.key === 'max_risk_per_trade_pct' ? { ...l, value: 12 } : l,
    )
    expect(riskLimitFor(edited, 'max_risk_per_trade_pct')).toBe(12)
    expect(riskLimitFor(RISK_LIMITS, 'max_risk_per_trade_pct')).toBe(7)
  })

  it('returns null for a key that is absent rather than substituting a number', () => {
    expect(riskLimitFor([], 'max_risk_per_trade_pct')).toBeNull()
  })
})

describe('auditEntry', () => {
  it('records the previous value, the new value, and when', () => {
    const at = new Date('2026-08-18T14:02:00Z')
    const entry = auditEntry('risk', 'max_risk_per_trade_pct', '7', '10', at)

    expect(entry.category).toBe('risk')
    expect(entry.field).toBe('max_risk_per_trade_pct')
    expect(entry.previousValue).toBe('7')
    expect(entry.newValue).toBe('10')
    expect(entry.time).toBe('2026-08-18T14:02:00.000Z')
  })

  it('does not collide with the ids already in the seeded log', () => {
    const entry = auditEntry('risk', 'max_daily_loss_pct', '20', '15', new Date())
    expect(AUDIT_LOG.some((e) => e.id === entry.id)).toBe(false)
  })
})

/** The log stores field *keys*, so it stays a record of what changed rather
 * than of how the UI worded it that week. That only works if something can
 * turn a key back into a label. */
describe('auditFieldLabel', () => {
  it('resolves a risk limit key to its label', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'risk', field: 'max_risk_per_trade_pct' })).toBe(
      'Max risk per trade',
    )
  })

  it('resolves a feed key to its label', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'feed', field: 'stockHistorical' })).toBe(
      'Equity bars (historical)',
    )
  })

  /** The engine keys a feed audit row by its **env var**, not by the
   * camelCase key the feed list uses (`settings.py` writes
   * `field=meta.env_var`). Matching on the key alone printed
   * `ALPACA_STOCK_FEED_HISTORICAL` in a column that promises readable
   * settings. Both are true names for the same row. */
  it('resolves a feed row the engine keyed by its env var', () => {
    expect(
      auditFieldLabel({
        ...AUDIT_LOG[0],
        category: 'feed',
        field: 'ALPACA_STOCK_FEED_HISTORICAL',
      }),
    ).toBe('Equity bars (historical)')
  })

  /** Resolved against what the server served, not against the fixture, so a
   * label the engine renames does not silently keep the old wording. */
  it('prefers the catalogue it is given over the fixtures', () => {
    const limits = [{ ...limit('max_risk_per_trade_pct'), label: 'Per-trade ceiling' }]
    expect(
      auditFieldLabel({ ...AUDIT_LOG[0], category: 'risk', field: 'max_risk_per_trade_pct' }, {
        limits,
      }),
    ).toBe('Per-trade ceiling')
  })

  it('resolves a notification route to its event and channel', () => {
    const field = notificationAuditField('order_filled', 'discord')
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'notification', field })).toBe(
      'Order filled — Discord',
    )
  })

  /** Decision 21: a manual watch is keyed by its ticker. Without its own
   * branch it fell through to the notification split and printed the bare
   * ticker, with nothing to say the row was a watch at all. */
  it('names a watchlist row as a watch on its ticker', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'watchlist', field: 'PLTR' })).toBe(
      'News watchlist — PLTR',
    )
  })

  /** Decision 13: a status transition is keyed by the source it moved. */
  it('names a sentiment row by the source whose status changed', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'sentiment', field: 'massive' })).toBe(
      'Sentiment source — Massive',
    )
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'sentiment', field: 'rules' })).toBe(
      'Sentiment source — Corollary rules',
    )
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'sentiment', field: 'llm' })).toBe(
      'Sentiment source — llm',
    )
  })

  it('falls back to the raw key rather than rendering nothing', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'risk', field: 'who_knows' })).toBe('who_knows')
  })

  it('covers every field the seeded log actually contains', () => {
    for (const entry of AUDIT_LOG) {
      expect(auditFieldLabel(entry)).not.toBe(entry.field)
    }
  })
})

/** The Basic plan does not merely return empty data for a feed it cannot
 * serve — CLAUDE.md notes it returns an auth error. So the selector has to
 * mark those values as unavailable rather than let you pick one and debug
 * the failure later. */
describe('feedOptionsFor', () => {
  it('gates OPRA options behind the paid plan', () => {
    const basic = feedOptionsFor('options', 'basic')
    expect(basic.find((o) => o.value === 'opra')?.requiresUpgrade).toBe(true)
    expect(basic.find((o) => o.value === 'indicative')?.requiresUpgrade).toBe(false)

    const paid = feedOptionsFor('options', 'algo_trader_plus')
    expect(paid.find((o) => o.value === 'opra')?.requiresUpgrade).toBe(false)
  })

  it('gates real-time SIP behind the paid plan', () => {
    const basic = feedOptionsFor('stockRealtime', 'basic')
    expect(basic.find((o) => o.value === 'sip')?.requiresUpgrade).toBe(true)
  })

  it('leaves historical SIP free on Basic, because it is', () => {
    const basic = feedOptionsFor('stockHistorical', 'basic')
    expect(basic.find((o) => o.value === 'sip')?.requiresUpgrade).toBe(false)
  })

  it('offers a value for every feed the fixture ships', () => {
    for (const feed of DATA_FEEDS) {
      const options = feedOptionsFor(feed.key, 'basic')
      expect(options.some((o) => o.value === feed.value)).toBe(true)
    }
  })

  it('never marks the currently configured value as needing an upgrade', () => {
    for (const feed of DATA_FEEDS) {
      const current = feedOptionsFor(feed.key, 'basic').find((o) => o.value === feed.value)
      expect(current?.requiresUpgrade).toBe(false)
    }
  })
})

describe('feedWarning', () => {
  it('warns when historical bars are set to IEX, because thresholds are read against them', () => {
    expect(feedWarning('stockHistorical', 'iex')).not.toBeNull()
    expect(feedWarning('stockHistorical', 'sip')).toBeNull()
  })

  it('does not warn about real-time IEX, which is the only free option', () => {
    expect(feedWarning('stockRealtime', 'iex')).toBeNull()
  })
})

/** Silencing both channels on one of these routes a rule-9 alert to nowhere.
 * The events are still editable — they just pass through a confirm that says
 * what stops arriving. */
describe('isCriticalEvent', () => {
  it('covers rejections, the daily loss halt, and the dead-man’s switch', () => {
    expect(isCriticalEvent('order_rejected')).toBe(true)
    expect(isCriticalEvent('daily_loss_halt')).toBe(true)
    expect(isCriticalEvent('engine_error')).toBe(true)
  })

  it('does not treat an ordinary fill or a recommendation as critical', () => {
    expect(isCriticalEvent('order_filled')).toBe(false)
    expect(isCriticalEvent('recommendations_ready')).toBe(false)
    expect(isCriticalEvent('strategy_promotion')).toBe(false)
    expect(isCriticalEvent('price_alert')).toBe(false)
  })

  /** A stop firing is a loss behaving correctly, not a system failure —
   * the same distinction CLAUDE.md draws between `bearish` and `error`. */
  it('does not treat a stop loss as critical', () => {
    expect(isCriticalEvent('stop_loss_hit')).toBe(false)
    // PRD §10's three only — a human halt or a config change is not an alarm.
    for (const e of ['operator_halt', 'operator_resume', 'risk_limits_changed', 'data_feeds_changed', 'notification_routes_changed', 'watchlist_changed'] as const) {
      expect(isCriticalEvent(e)).toBe(false)
    }
  })

  it('classifies every routable event without throwing', () => {
    const events = Object.keys(NOTIFICATION_EVENT_LABEL) as NotificationEvent[]
    for (const event of events) {
      expect(typeof isCriticalEvent(event)).toBe('boolean')
    }
  })
})

/* ---------------------------------------------------------------------- *
 * The sentiment self-audit. The status is the server's: nothing here may
 * compute a demotion, so these pin display arithmetic, the mapping from a
 * refused POST to what the page says, and that the thresholds the page
 * states in words are the engine's.
 * ---------------------------------------------------------------------- */

/** These mirror `corollary/data/news/audit.py` (`DEMOTE_BELOW_PCT`,
 * `REPROMOTE_AT_PCT`, `DEMOTION_FLOOR`, `REPROMOTION_FLOOR`). The
 * cross-language pin is `test_sentiment_thresholds_match_the_engine` in
 * `tests/api/test_schema_contract.py`, which reads `settings.ts`'s source —
 * Vite's fs allow-list refuses a `?raw` import of a file outside `web/`. */
describe('the thresholds the page states', () => {
  it('are decision 13, with a dead band between them', () => {
    expect(SENTIMENT_FLOOR).toBe(52)
    expect(SENTIMENT_REPROMOTE).toBe(66)
    expect(SENTIMENT_DEMOTE_SAMPLE).toBe(30)
    expect(SENTIMENT_REPROMOTE_SAMPLE).toBe(100)
    expect(SENTIMENT_REPROMOTE).toBeGreaterThan(SENTIMENT_FLOOR)
  })
})

describe('accuracyTenths', () => {
  it('is correct over graded, neutral in neither', () => {
    expect(accuracyTenths({ graded: 200, correct: 123, neutral: 999 })).toBe(615)
  })

  it('is null with nothing graded — 0 of 0 is not 0%', () => {
    expect(accuracyTenths({ graded: 0, correct: 0, neutral: 12 })).toBeNull()
  })

  /** The engine compares `correct * 100 < 52 * graded` in integers. A figure
   * that rounded would print 52.0% beside a source just demoted for being
   * under 52. */
  it('truncates, so a figure never rounds across a threshold', () => {
    const tenths = accuracyTenths({ graded: 25_000, correct: 12_999, neutral: 0 })
    expect(tenths).toBe(519)
    expect(formatTenthsPct(tenths!)).toBe('51.9%')
    expect(formatTenthsPct(accuracyTenths({ graded: 50, correct: 33, neutral: 0 })!)).toBe('66.0%')
  })
})

describe('isInsufficientSample', () => {
  it('is below the floor, and the floor itself is enough', () => {
    expect(isInsufficientSample({ graded: 29, correct: 20, neutral: 0 }, 30)).toBe(true)
    expect(isInsufficientSample({ graded: 30, correct: 20, neutral: 0 }, 30)).toBe(false)
  })
})

describe('actingCell', () => {
  const { populated } = SENTIMENT_AUDIT_STATES

  it('reads trailing_28d for a source counting toward demotion', () => {
    const rules = populated.sources[0]
    expect(actingCell(rules, '1h')?.basis).toBe('trailing_28d')
    expect(cellCounts(actingCell(rules, '1h')).graded).toBe(212)
  })

  /** A demoted source is judged on what it has done since; its trailing rows
   * are served too, and reading them would show the figure that demoted it. */
  it('reads since_demotion for a demoted source', () => {
    const massive = populated.sources[1]
    expect(actingCell(massive, '1h')?.basis).toBe('since_demotion')
    expect(cellCounts(actingCell(massive, '1h')).graded).toBe(41)
  })

  it('is null, and counts as zero, with no row on the basis', () => {
    const rules = SENTIMENT_AUDIT_STATES.unaudited.sources[0]
    expect(actingCell(rules, '1d')).toBeNull()
    expect(cellCounts(null)).toEqual({ graded: 0, correct: 0, neutral: 0 })
  })
})

describe('demotedSources', () => {
  it('takes the server status, not the accuracy', () => {
    expect(demotedSources(SENTIMENT_AUDIT_STATES.populated.sources).map((s) => s.source)).toEqual([
      'massive',
    ])
  })

  /** An active source whose figure is under 52% is still active until the
   * engine says otherwise, and the banner says what the engine did, not what
   * the page would have done. */
  it('never demotes from a low figure on its own', () => {
    const rules = SENTIMENT_AUDIT_STATES.populated.sources[0]
    const low = {
      ...rules,
      accuracy: rules.accuracy.map((a) => ({
        ...a,
        combined: { graded: 300, correct: 30, neutral: 0 },
      })),
    }
    expect(demotedSources([low])).toEqual([])
  })

  it('does not read a missing status row as anything but missing', () => {
    expect(demotedSources(SENTIMENT_AUDIT_STATES.statusMissing.sources)).toEqual([])
  })
})

describe('sourceStatusKind', () => {
  it('names missing ahead of the fallback it is served with', () => {
    const [rules, massive] = SENTIMENT_AUDIT_STATES.statusMissing.sources
    expect(sourceStatusKind(massive)).toBe('missing')
    expect(sourceStatusKind(rules)).toBe('active')
  })

  it('reads never-audited and demoted', () => {
    expect(sourceStatusKind(SENTIMENT_AUDIT_STATES.unaudited.sources[0])).toBe('unaudited')
    expect(sourceStatusKind(SENTIMENT_AUDIT_STATES.populated.sources[1])).toBe('demoted')
  })
})

describe('repromotionProgress', () => {
  it('states each window against the re-promotion floor', () => {
    expect(repromotionProgress(SENTIMENT_AUDIT_STATES.populated.sources[1])).toEqual([
      'since demotion: 41 of 100 in the 1h window',
      'since demotion: 37 of 100 in the 1d window',
    ])
  })

  it('is empty for a source not counting toward re-promotion', () => {
    expect(repromotionProgress(SENTIMENT_AUDIT_STATES.populated.sources[0])).toEqual([])
  })
})

describe('nothingAuditedYet', () => {
  it('is true only with no succeeded run', () => {
    expect(nothingAuditedYet(SENTIMENT_AUDIT_STATES.unaudited)).toBe(true)
    expect(nothingAuditedYet(SENTIMENT_AUDIT_STATES.populated)).toBe(false)
  })
})

describe('auditTriggerRefusal', () => {
  const url = '/api/settings/sentiment/audit'
  const at = Date.parse('2026-10-14T15:30:00Z')
  const err = (status: number, code: string, retryAfterSeconds: number | null = null) =>
    new ApiError({ status, code, message: `server says ${status}`, url, retryAfterSeconds })

  it('times a 429 from Retry-After, counted from when it was sent', () => {
    const refusal = auditTriggerRefusal(err(429, SENTIMENT_AUDIT_RATE_LIMITED, 1800), {
      requestedAt: at,
      manualAvailableAt: '2026-10-14T17:00:00Z',
    })
    expect(refusal).toEqual({
      kind: 'rate_limited',
      availableAt: '2026-10-14T16:00:00.000Z',
      message: 'server says 429',
    })
  })

  it('falls back to manualAvailableAt with no Retry-After', () => {
    const refusal = auditTriggerRefusal(err(429, SENTIMENT_AUDIT_RATE_LIMITED), {
      requestedAt: at,
      manualAvailableAt: '2026-10-14T16:20:00Z',
    })
    expect(refusal).toMatchObject({ kind: 'rate_limited', availableAt: '2026-10-14T16:20:00Z' })
  })

  it('sorts 409, 503 and 500 apart and keeps the server sentence', () => {
    const ctx = { requestedAt: at, manualAvailableAt: null }
    expect(auditTriggerRefusal(err(409, SENTIMENT_AUDIT_RUNNING), ctx)).toEqual({
      kind: 'running',
      message: 'server says 409',
    })
    expect(auditTriggerRefusal(err(503, SENTIMENT_AUDIT_UNAVAILABLE), ctx)).toEqual({
      kind: 'unavailable',
      message: 'server says 503',
    })
    expect(auditTriggerRefusal(err(500, 'internal_error'), ctx)).toEqual({
      kind: 'failed',
      message: 'server says 500',
    })
  })

  it('says the engine did not answer when it did not', () => {
    const refusal = auditTriggerRefusal(err(0, NETWORK_UNREACHABLE), {
      requestedAt: at,
      manualAvailableAt: null,
    })
    expect(refusal.kind).toBe('failed')
    expect(refusal.message).toMatch(/did not answer/)
  })
})
