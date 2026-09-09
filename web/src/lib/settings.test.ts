import { describe, it, expect } from 'vitest'
import {
  AUDIT_LOG,
  DATA_FEEDS,
  NOTIFICATION_EVENT_LABEL,
  RISK_LIMITS,
  SENTIMENT_ACCURACY,
  type NotificationEvent,
  type RiskLimit,
} from './mockData'
import {
  SENTIMENT_FLOOR,
  auditEntry,
  auditFieldLabel,
  demotedSources,
  feedOptionsFor,
  feedWarning,
  isCriticalEvent,
  isRaise,
  notificationAuditField,
  riskDollars,
  riskLimitFor,
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

  it('resolves a notification route to its event and channel', () => {
    const field = notificationAuditField('order_filled', 'discord')
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'notification', field })).toBe(
      'Order filled — Discord',
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
  })

  it('classifies every routable event without throwing', () => {
    const events = Object.keys(NOTIFICATION_EVENT_LABEL) as NotificationEvent[]
    for (const event of events) {
      expect(typeof isCriticalEvent(event)).toBe('boolean')
    }
  })
})

/** PRD.md §9: below 52% the source is demoted from a scanner input to
 * display-only. "Below" is strict — 52% exactly is not demoted, and an
 * off-by-one here silently drops a working signal out of the scanner. */
describe('demotedSources', () => {
  it('demotes a source that is below the floor in either window', () => {
    const demoted = demotedSources(SENTIMENT_ACCURACY)
    expect(demoted.map((d) => d.source)).toEqual(['StockTwits'])
  })

  it('treats the floor itself as passing, not failing', () => {
    const at = [{ source: 'X', tier: 't', accuracy1h: SENTIMENT_FLOOR, accuracy1d: SENTIMENT_FLOOR }]
    expect(demotedSources(at)).toEqual([])

    const below = [{ source: 'X', tier: 't', accuracy1h: SENTIMENT_FLOOR - 1, accuracy1d: 90 }]
    expect(demotedSources(below)).toHaveLength(1)
  })

  it('demotes on a failing one-day figure too, not only the one-hour one', () => {
    const rows = [{ source: 'X', tier: 't', accuracy1h: 90, accuracy1d: 40 }]
    expect(demotedSources(rows)).toHaveLength(1)
  })

  it('returns nothing when every source clears the floor', () => {
    const rows = SENTIMENT_ACCURACY.filter((r) => r.source !== 'StockTwits')
    expect(demotedSources(rows)).toEqual([])
  })
})
