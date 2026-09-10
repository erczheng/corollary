import type { AccountMode, Notification, NotificationEvent, NotificationRoute } from './types'
import type { NotificationChannel } from './settings'

/** Everything the bell needs, as pure functions with no React in them.
 *
 * Separate from `settings.ts` because the two answer different questions.
 * Settings decides **what should be delivered** — the routing matrix, and
 * which events are too important to silence quietly. This file decides **how
 * a delivered notification reads**: its severity, whose book it belongs to,
 * and whether it has been seen.
 */

export type NotificationSeverity = 'critical' | 'warning' | 'info'

/** Severity is a property of the event type, not of the individual
 * notification — a fill is a fill whether it made money or lost it.
 *
 * The one that needs saying: `stop_loss_hit` is a **warning**, not critical.
 * A stop firing is an exit doing precisely what it was configured to do, and
 * CLAUDE.md is explicit that a losing position must never be rendered as a
 * system failure. Critical is reserved for things that mean the system is not
 * doing what you asked: an order refused by a rule, a session halted on loss,
 * a dead connection. */
const SEVERITY: Record<NotificationEvent, NotificationSeverity> = {
  order_rejected: 'critical',
  daily_loss_halt: 'critical',
  engine_error: 'critical',
  stop_loss_hit: 'warning',
  order_filled: 'info',
  price_alert: 'info',
  recommendations_ready: 'info',
  strategy_promotion: 'info',
}

export function severityFor(event: NotificationEvent): NotificationSeverity {
  return SEVERITY[event]
}

/** Text colour per severity.
 *
 * `error` for critical — a rule outcome or a system fault. Never `bearish`,
 * which belongs to losses and is a different colour on purpose. `info` takes
 * `on-surface-variant` (8.9:1) rather than `neutral`, which is the same value
 * as `outline` at 4.27:1 and is not a text colour however muted it looks. */
export const SEVERITY_CLASS: Record<NotificationSeverity, string> = {
  critical: 'text-error',
  warning: 'text-caution',
  info: 'text-on-surface-variant',
}

/** Background for the severity dot beside a row. Same three tokens as fills
 * rather than as text, so the dot reads at 8px where a text colour would not. */
export const SEVERITY_DOT_CLASS: Record<NotificationSeverity, string> = {
  critical: 'bg-error',
  warning: 'bg-caution',
  info: 'bg-outline',
}

/** Whether an event is routed to a channel.
 *
 * **Fails open.** A missing route is a misconfiguration, and the safe result
 * of a misconfiguration is a notification nobody needed rather than a silent
 * drop — especially since half these events only fire when something has
 * already gone wrong.
 *
 * Applied when an event is **emitted**, never when the panel is read.
 * Filtering at read time would make unchecking a route retroactively erase
 * history, which is a different and false claim: you *did* receive those
 * fills, and the log of what happened must not change because you later
 * changed what you want to hear about. */
export function routedTo(
  routes: NotificationRoute[],
  event: NotificationEvent,
  channel: NotificationChannel,
): boolean {
  const route = routes.find((r) => r.event === event)
  if (!route) return true
  return route[channel]
}

/** The notifications belonging to one book.
 *
 * `account === null` is an event that belongs to no book — an engine fault, a
 * batch of new recommendations — and it shows in both. Scoping those away
 * would hide exactly the events that matter most, since a dead-man's switch
 * firing is not paper's news or cash's, it is the terminal's.
 *
 * Filters rather than sorts: the feed is stored newest-first and every reader
 * shares the same array, so sorting here would leave one view's ordering
 * behind in everyone else's data. */
export function visibleNotifications(
  all: Notification[],
  mode: AccountMode,
): Notification[] {
  return all.filter((n) => n.account === mode || n.account === null)
}

/** Unread count for the badge, scoped to the book on screen.
 *
 * Counting across both accounts would put a number on the bell that the panel
 * beneath it cannot account for — you would open it, read everything, and
 * still see a badge. */
export function unreadCount(all: Notification[], mode: AccountMode): number {
  return visibleNotifications(all, mode).filter((n) => !n.read).length
}

/** One new notification.
 *
 * `key` discriminates events of the same type at the same instant — a
 * position id, a symbol, a contract. The id follows the composite shape the
 * store already uses for orders and activity rows (`act-…-${at}`), which is
 * deterministic: a seeded session replays with identical ids, so nothing here
 * makes a snapshot test flake.
 *
 * Stores no title. The heading comes from `NOTIFICATION_EVENT_LABEL[event]`,
 * so there is exactly one place where "Order filled" is worded. */
export function buildNotification(
  event: NotificationEvent,
  key: string,
  detail: string,
  account: AccountMode | null,
  at: string,
): Notification {
  return {
    id: `notif-${event}-${key}-${at}`,
    time: at,
    event,
    detail,
    read: false,
    account,
  }
}
