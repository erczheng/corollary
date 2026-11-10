import { describe, it, expect } from 'vitest'
import { AUDIT_LOG, DATA_FEEDS, RISK_LIMITS, SENTIMENT_AUDIT_STATES } from './mockData'
import {
  NOTIFICATION_EVENT_LABEL,
  type NotificationEvent,
  type RiskLimit,
  type SentimentSourceAudit,
} from './types'
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
  SESSIONS_NOT_COUNTED,
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
  isShortOfSessions,
  nothingAuditedYet,
  notificationAuditField,
  repromotionProgress,
  riskDollars,
  riskLimitFor,
  sessionsFact,
  shortCellLead,
  sourceStatusKind,
  statedSessionFloor,
  validateRiskLimit,
  windowProgress,
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

  /** Rule 5's confirm/end rows are `category: 'account'`, `field:
   * 'cash_session'`. Falling through to the notification split below printed
   * the raw `cash_session` key in a column that promises readable settings. */
  it('labels a Cash session row', () => {
    expect(auditFieldLabel({ ...AUDIT_LOG[0], category: 'account', field: 'cash_session' })).toBe(
      'Cash session',
    )
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
    expect(accuracyTenths({ graded: 200, correct: 123, neutral: 999, sessions: 9 })).toBe(615)
  })

  it('is null with nothing graded — 0 of 0 is not 0%', () => {
    expect(accuracyTenths({ graded: 0, correct: 0, neutral: 12, sessions: 0 })).toBeNull()
  })

  /** The engine compares `correct * 100 < 52 * graded` in integers. A figure
   * that rounded would print 52.0% beside a source just demoted for being
   * under 52. */
  it('truncates, so a figure never rounds across a threshold', () => {
    const tenths = accuracyTenths({ graded: 25_000, correct: 12_999, neutral: 0, sessions: 20 })
    expect(tenths).toBe(519)
    expect(formatTenthsPct(tenths!)).toBe('51.9%')
    expect(formatTenthsPct(accuracyTenths({ graded: 50, correct: 33, neutral: 0, sessions: 6 })!)).toBe('66.0%')
  })
})

describe('isInsufficientSample', () => {
  it('is below the floor, and the floor itself is enough', () => {
    expect(isInsufficientSample({ graded: 29, correct: 20, neutral: 0, sessions: 5 }, 30)).toBe(true)
    expect(isInsufficientSample({ graded: 30, correct: 20, neutral: 0, sessions: 5 }, 30)).toBe(false)
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
    expect(cellCounts(null)).toEqual({ graded: 0, correct: 0, neutral: 0, sessions: 0 })
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
        combined: { graded: 300, correct: 30, neutral: 0, sessions: 12 },
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
      'since demotion, 1h window: 41 of 100 labels, 4 of 5 sessions',
      'since demotion, 1d window: 37 of 100 labels, 4 of 5 sessions',
    ])
  })

  it('is empty for a source not counting toward re-promotion', () => {
    expect(repromotionProgress(SENTIMENT_AUDIT_STATES.populated.sources[0])).toEqual([])
  })

  /** "100 of 100 labels" alone would promise a re-promotion the session floor
   * is still holding back, so the sessions clause rides on every line. */
  it('states the session floor beside the label floor, from the server', () => {
    const massive = SENTIMENT_AUDIT_STATES.populated.sources[1]
    const atLabels: SentimentSourceAudit = {
      ...massive,
      eligibility: {
        ...massive.eligibility,
        sessionFloor: 7,
        windows: massive.eligibility.windows.map((w) => ({
          ...w,
          graded: 100,
          needed: 0,
          sessions: 3,
          sessionsNeeded: 4,
        })),
      },
    }
    expect(repromotionProgress(atLabels)).toEqual([
      'since demotion, 1h window: 100 of 100 labels, 3 of 7 sessions',
      'since demotion, 1d window: 100 of 100 labels, 3 of 7 sessions',
    ])
  })

  it('says in words that a pre-floor row has no session count', () => {
    const massive = SENTIMENT_AUDIT_STATES.populated.sources[1]
    const preFloor: SentimentSourceAudit = {
      ...massive,
      eligibility: {
        ...massive.eligibility,
        windows: massive.eligibility.windows.map((w) => ({
          ...w,
          sessions: null,
          sessionsNeeded: null,
        })),
      },
    }
    const lines = repromotionProgress(preFloor)
    expect(lines[0]).toBe(`since demotion, 1h window: 41 of 100 labels, ${SESSIONS_NOT_COUNTED}`)
    for (const line of lines) expect(line).not.toMatch(/\d+ of \d+ sessions/)
  })
})

/* ---------------------------------------------------------------------- *
 * The five-session floor (spec R3, R9). These format what the server sent
 * and decide nothing: none of them returns, or is, an eligibility.
 * ---------------------------------------------------------------------- */

describe('windowProgress', () => {
  it('is the server’s entry for the window', () => {
    const rules = SENTIMENT_AUDIT_STATES.sessionsShort.sources[0]
    expect(windowProgress(rules, '1h')).toEqual({
      window: '1h',
      graded: 31,
      needed: 0,
      sessions: 2,
      sessionsNeeded: 3,
    })
  })

  it('is null for a window the server sent no entry for', () => {
    const rules = SENTIMENT_AUDIT_STATES.populated.sources[0]
    const oneWindow: SentimentSourceAudit = {
      ...rules,
      eligibility: {
        ...rules.eligibility,
        windows: rules.eligibility.windows.filter((w) => w.window === '1h'),
      },
    }
    expect(windowProgress(oneWindow, '1d')).toBeNull()
  })
})

describe('isShortOfSessions', () => {
  const progress = (sessions: number | null, sessionsNeeded: number | null) => ({
    window: '1h' as const,
    graded: 40,
    needed: 0,
    sessions,
    sessionsNeeded,
  })

  it('reads the server’s sessionsNeeded, and the floor itself is enough', () => {
    expect(isShortOfSessions(progress(4, 1))).toBe(true)
    expect(isShortOfSessions(progress(5, 0))).toBe(false)
  })

  /** It compares nothing to a constant: a server floor of 8 met at 8. */
  it('follows the server, not a client-side 5', () => {
    expect(isShortOfSessions(progress(8, 0))).toBe(false)
    expect(isShortOfSessions(progress(6, 2))).toBe(true)
  })

  /** R3: no reader may treat NULL as eligible. */
  it('is short for a pre-floor row, and for a window with no entry', () => {
    expect(isShortOfSessions(progress(null, null))).toBe(true)
    expect(isShortOfSessions(null)).toBe(true)
  })
})

describe('sessionsFact', () => {
  it('is the count against the server’s floor', () => {
    const rules = SENTIMENT_AUDIT_STATES.sessionsShort.sources[0]
    expect(sessionsFact(windowProgress(rules, '1h'), rules.eligibility.sessionFloor)).toBe(
      '2 of 5 sessions',
    )
    expect(sessionsFact(windowProgress(rules, '1h'), 6)).toBe('2 of 6 sessions')
  })

  /** Nothing has run: a real zero, not the pre-floor null. */
  it('says 0 of the floor when nothing has been graded', () => {
    const rules = SENTIMENT_AUDIT_STATES.unaudited.sources[0]
    expect(rules.eligibility.asOfRunId).toBeNull()
    expect(sessionsFact(windowProgress(rules, '1h'), rules.eligibility.sessionFloor)).toBe(
      '0 of 5 sessions',
    )
  })

  it('says a pre-floor row was not counted, with no number at all', () => {
    const rules = SENTIMENT_AUDIT_STATES.preSessionFloor.sources[0]
    const fact = sessionsFact(windowProgress(rules, '1h'), rules.eligibility.sessionFloor)
    expect(fact).toBe(SESSIONS_NOT_COUNTED)
    expect(fact).not.toMatch(/\d/)
  })

  it('says a missing window was not reported', () => {
    expect(sessionsFact(null, 5)).toBe('sessions not reported')
  })
})

/** "insufficient" is a count found short; a pre-floor row was never
 * counted, so its lead says that instead. */
describe('shortCellLead', () => {
  it('is "not measured" for a row written before the session floor', () => {
    const rules = SENTIMENT_AUDIT_STATES.preSessionFloor.sources[0]
    expect(shortCellLead(windowProgress(rules, '1h'))).toBe('not measured')
    expect(shortCellLead(windowProgress(rules, '1d'))).toBe('not measured')
  })

  it('is "insufficient sample" for any counted row, and a missing one', () => {
    const short = SENTIMENT_AUDIT_STATES.sessionsShort.sources[0]
    expect(shortCellLead(windowProgress(short, '1h'))).toBe('insufficient sample')
    const never = SENTIMENT_AUDIT_STATES.unaudited.sources[0]
    expect(shortCellLead(windowProgress(never, '1h'))).toBe('insufficient sample')
    expect(shortCellLead(null)).toBe('insufficient sample')
  })
})

describe('statedSessionFloor', () => {
  it('is the floor every source carries', () => {
    expect(statedSessionFloor(SENTIMENT_AUDIT_STATES.populated.sources)).toBe(5)
  })

  it('is null with no sources, or with two that disagree', () => {
    expect(statedSessionFloor([])).toBeNull()
    const [rules, massive] = SENTIMENT_AUDIT_STATES.populated.sources
    const other = { ...massive, eligibility: { ...massive.eligibility, sessionFloor: 6 } }
    expect(statedSessionFloor([rules, other])).toBeNull()
  })
})

/** The fixtures play the server, so they must hold what the server holds:
 * `eligible` needs both floors in both windows, and a pre-floor row never
 * is. Pinned here so a fixture edit cannot quietly draw an impossible state. */
describe('the session-floor fixtures', () => {
  it('cover every state, with eligible as the server would send it', () => {
    const { unaudited, populated, sessionsShort, preSessionFloor } = SENTIMENT_AUDIT_STATES
    // asOfRunId null: zero sessions, the whole floor still needed.
    expect(unaudited.sources[0].eligibility.windows.map((w) => w.sessionsNeeded)).toEqual([5, 5])
    // At the floor exactly, and eligible.
    expect(populated.sources[0].eligibility.windows[0].sessions).toBe(5)
    expect(populated.sources[0].eligibility.eligible).toBe(true)
    // Label floor met, session floor not.
    expect(sessionsShort.sources[0].eligibility.windows.every((w) => w.needed === 0)).toBe(true)
    expect(sessionsShort.sources[0].eligibility.eligible).toBe(false)
    // Pre-floor: null together, never eligible.
    for (const w of preSessionFloor.sources[0].eligibility.windows) {
      expect([w.sessions, w.sessionsNeeded]).toEqual([null, null])
    }
    expect(preSessionFloor.sources[0].eligibility.eligible).toBe(false)
  })

  it('keep sessions within the engine’s invariants on every cell', () => {
    for (const state of Object.values(SENTIMENT_AUDIT_STATES)) {
      for (const source of state.sources) {
        for (const cell of source.accuracy) {
          const { combined, watch, discovery } = cell
          for (const c of [combined, watch, discovery]) {
            if (c.sessions === null) continue
            expect(c.sessions).toBeLessThanOrEqual(c.graded)
            expect(c.sessions === 0).toBe(c.graded === 0)
          }
          // Sessions are a distinct union, never a sum.
          if (combined.sessions !== null && watch.sessions !== null && discovery.sessions !== null) {
            expect(combined.sessions).toBeGreaterThanOrEqual(Math.max(watch.sessions, discovery.sessions))
            expect(combined.sessions).toBeLessThanOrEqual(watch.sessions + discovery.sessions)
          }
        }
      }
    }
  })

  /** A run writes `sessions` on every row or, before the floor existed, on
   * none. One state mixing NULL and counted rows from the same run (U3 C1)
   * draws something the engine cannot produce. */
  it('never mix counted and uncounted rows from one run', () => {
    for (const [name, state] of Object.entries(SENTIMENT_AUDIT_STATES)) {
      const byRun = new Map<number, Set<boolean>>()
      for (const source of state.sources) {
        const run = source.eligibility.asOfRunId
        if (run === null) continue
        const seen = byRun.get(run) ?? new Set<boolean>()
        for (const cell of source.accuracy) {
          for (const c of [cell.combined, cell.watch, cell.discovery]) seen.add(c.sessions === null)
        }
        for (const w of source.eligibility.windows) seen.add(w.sessions === null)
        byRun.set(run, seen)
      }
      for (const [run, seen] of byRun) {
        expect({ state: name, run, mixed: seen.size > 1 }).toEqual({ state: name, run, mixed: false })
      }
    }
  })
})

/** R9: `lastRun` and `accuracyRun` are the newest **non-voided** runs, and
 * `voidedRuns` is newest void first. */
describe('the voided-run fixtures', () => {
  it('never report a voided run as the last run or the figures’ run', () => {
    for (const [name, state] of Object.entries(SENTIMENT_AUDIT_STATES)) {
      const voided = new Set(state.voidedRuns.map((v) => v.id))
      expect({ state: name, last: voided.has(state.lastRun?.id ?? -1) }).toEqual({
        state: name,
        last: false,
      })
      expect({ state: name, figures: voided.has(state.accuracyRun?.id ?? -1) }).toEqual({
        state: name,
        figures: false,
      })
      for (const source of state.sources) {
        expect(voided.has(source.eligibility.asOfRunId ?? -1)).toBe(false)
      }
    }
  })

  it('list voids newest first, and carry R5’s reason verbatim', () => {
    const { voided, voidedTwice } = SENTIMENT_AUDIT_STATES
    expect(voided.voidedRuns).toHaveLength(1)
    expect(voided.voidedRuns[0].reason).toBe(
      'graded under the reference rule the owner replaced on 2026-10-22, before that change merged (Q23a, 2026-11-09)',
    )
    const times = voidedTwice.voidedRuns.map((v) => Date.parse(v.voidedAt))
    expect(times).toHaveLength(2)
    expect(times[0]).toBeGreaterThan(times[1])
    // Only the latest non-voided run can be voided (R4), so the newer void is
    // the lower id here: server order, not id order.
    expect(voidedTwice.voidedRuns.map((v) => v.id)).toEqual([8, 9])
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
