import { describe, it, expect } from 'vitest'
import { NOTIFICATIONS, NOTIFICATION_ROUTES } from './mockData'
import { NOTIFICATION_EVENT_LABEL, type NotificationEvent } from './types'
import {
  SEVERITY_CLASS,
  SEVERITY_DOT_CLASS,
  buildNotification,
  routedTo,
  severityFor,
  unreadCount,
  visibleNotifications,
} from './notifications'

const EVENTS = Object.keys(NOTIFICATION_EVENT_LABEL) as NotificationEvent[]

describe('severityFor', () => {
  it('treats rejections, the loss halt, and the dead-man’s switch as critical', () => {
    expect(severityFor('order_rejected')).toBe('critical')
    expect(severityFor('daily_loss_halt')).toBe('critical')
    expect(severityFor('engine_error')).toBe('critical')
  })

  /** CLAUDE.md keeps `bearish` and `error` apart because a losing position is
   * not a system failure. A stop firing is the most ordinary loss there is —
   * the exit did its job — so it must not be dressed as an error. */
  it('treats a stop loss as a warning, never as critical', () => {
    expect(severityFor('stop_loss_hit')).toBe('warning')
    expect(SEVERITY_CLASS.warning).not.toBe(SEVERITY_CLASS.critical)
  })

  it('treats fills and informational events as info', () => {
    expect(severityFor('order_filled')).toBe('info')
    expect(severityFor('price_alert')).toBe('info')
    expect(severityFor('recommendations_ready')).toBe('info')
    expect(severityFor('strategy_promotion')).toBe('info')
  })

  it('classifies every routable event', () => {
    for (const event of EVENTS) {
      expect(['critical', 'warning', 'info']).toContain(severityFor(event))
    }
  })
})

/** These are token class strings, and the two traps CLAUDE.md names are both
 * live here: `error` is not `bearish`, and `neutral` is 4.27:1 and therefore
 * not a text colour at all. */
describe('severity classes', () => {
  it('uses error for critical and caution for warning, and never confuses them', () => {
    expect(SEVERITY_CLASS.critical).toContain('error')
    expect(SEVERITY_CLASS.warning).toContain('caution')
  })

  it('never renders a notification in bearish, which is for losses not failures', () => {
    for (const cls of Object.values(SEVERITY_CLASS)) {
      expect(cls).not.toContain('bearish')
      expect(cls).not.toContain('bullish')
    }
  })

  it('never uses bare neutral as a text colour', () => {
    for (const cls of Object.values(SEVERITY_CLASS)) {
      expect(cls).not.toBe('text-neutral')
    }
  })

  it('has a dot class for every severity', () => {
    for (const severity of ['critical', 'warning', 'info'] as const) {
      expect(SEVERITY_DOT_CLASS[severity]).toBeTruthy()
    }
  })
})

/** The gate is applied when an event is *emitted*, never when the panel is
 * read — see the note in notifications.ts. These tests only cover the lookup;
 * store.test.ts covers the suppression itself. */
describe('routedTo', () => {
  it('reads the configured channel for an event', () => {
    expect(routedTo(NOTIFICATION_ROUTES, 'order_filled', 'bell')).toBe(true)
    expect(routedTo(NOTIFICATION_ROUTES, 'recommendations_ready', 'discord')).toBe(false)
  })

  it('follows an edited route rather than the fixture default', () => {
    const edited = NOTIFICATION_ROUTES.map((r) =>
      r.event === 'order_filled' ? { ...r, bell: false } : r,
    )
    expect(routedTo(edited, 'order_filled', 'bell')).toBe(false)
  })

  /** Fails *open*. A missing route means a misconfiguration, and the safe
   * outcome of a misconfiguration is a notification you did not need rather
   * than a missing one you did — particularly for the events that only fire
   * when something has already gone wrong. */
  it('delivers when no route is configured at all', () => {
    expect(routedTo([], 'engine_error', 'bell')).toBe(true)
  })

  it('has a configured route for every event in the fixture', () => {
    for (const event of EVENTS) {
      expect(NOTIFICATION_ROUTES.some((r) => r.event === event)).toBe(true)
    }
  })
})

/** An engine error belongs to no book. Scoping it away because the other
 * account is selected would hide the one class of event you most need. */
describe('visibleNotifications', () => {
  it('shows the selected account’s notifications', () => {
    const visible = visibleNotifications(NOTIFICATIONS, 'paper')
    expect(visible.some((n) => n.account === 'paper')).toBe(true)
    expect(visible.some((n) => n.account === 'cash')).toBe(false)
  })

  it('shows account-less events in both books', () => {
    const engineError = NOTIFICATIONS.find((n) => n.event === 'engine_error')!
    expect(engineError.account).toBeNull()

    for (const mode of ['paper', 'cash'] as const) {
      expect(visibleNotifications(NOTIFICATIONS, mode).map((n) => n.id)).toContain(engineError.id)
    }
  })

  it('shows the cash account’s halt only in the cash book', () => {
    const halt = NOTIFICATIONS.find((n) => n.event === 'daily_loss_halt')!
    expect(halt.account).toBe('cash')

    expect(visibleNotifications(NOTIFICATIONS, 'cash').map((n) => n.id)).toContain(halt.id)
    expect(visibleNotifications(NOTIFICATIONS, 'paper').map((n) => n.id)).not.toContain(halt.id)
  })

  it('preserves newest-first order and does not sort in place', () => {
    const before = NOTIFICATIONS.map((n) => n.id)
    const visible = visibleNotifications(NOTIFICATIONS, 'paper')

    const times = visible.map((n) => n.time)
    expect([...times].sort().reverse()).toEqual(times)
    expect(NOTIFICATIONS.map((n) => n.id)).toEqual(before)
  })

  it('returns an empty array for an empty feed', () => {
    expect(visibleNotifications([], 'paper')).toEqual([])
  })
})

describe('unreadCount', () => {
  it('counts only unread notifications visible in the selected book', () => {
    const visible = visibleNotifications(NOTIFICATIONS, 'paper')
    const expected = visible.filter((n) => !n.read).length

    expect(unreadCount(NOTIFICATIONS, 'paper')).toBe(expected)
    expect(expected).toBeGreaterThan(0)
  })

  it('does not count the other account’s unread items', () => {
    const feed = [
      { ...NOTIFICATIONS[0], id: 'a', read: false, account: 'paper' as const },
      { ...NOTIFICATIONS[0], id: 'b', read: false, account: 'cash' as const },
    ]
    expect(unreadCount(feed, 'paper')).toBe(1)
  })

  it('is zero when everything has been read', () => {
    const read = NOTIFICATIONS.map((n) => ({ ...n, read: true }))
    expect(unreadCount(read, 'paper')).toBe(0)
  })
})

describe('buildNotification', () => {
  it('arrives unread, timestamped, and scoped to the account given', () => {
    const at = '2026-08-18T14:02:00.000Z'
    const n = buildNotification('order_filled', 'pos-1', 'AAPL filled', 'paper', at)

    expect(n.read).toBe(false)
    expect(n.time).toBe(at)
    expect(n.event).toBe('order_filled')
    expect(n.account).toBe('paper')
    expect(n.detail).toBe('AAPL filled')
  })

  it('stores no title of its own, so the label has one home', () => {
    const n = buildNotification('order_filled', 'pos-1', 'detail', 'paper', '2026-08-18T14:02:00.000Z')
    expect(Object.keys(n)).not.toContain('title')
  })

  it('builds distinct ids for two events on different positions at the same instant', () => {
    const at = '2026-08-18T14:02:00.000Z'
    const a = buildNotification('order_filled', 'pos-1', 'x', 'paper', at)
    const b = buildNotification('order_filled', 'pos-2', 'y', 'paper', at)

    expect(a.id).not.toBe(b.id)
  })

  it('does not collide with the seeded feed', () => {
    const n = buildNotification('order_filled', 'pos-1', 'x', 'paper', new Date().toISOString())
    expect(NOTIFICATIONS.some((seed) => seed.id === n.id)).toBe(false)
  })

  it('accepts a null account for engine-level events', () => {
    const n = buildNotification('engine_error', 'stream', 'disconnected', null, '2026-08-18T14:02:00.000Z')
    expect(n.account).toBeNull()
  })
})
