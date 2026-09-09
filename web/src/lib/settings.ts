import {
  DATA_FEEDS,
  NOTIFICATION_EVENT_LABEL,
  RISK_LIMITS,
  type AuditCategory,
  type AuditLogEntry,
  type DataPlan,
  type FeedKey,
  type NotificationEvent,
  type RiskLimit,
  type RiskLimitKey,
  type SentimentAccuracy,
} from './mockData'

/** Every rule the Settings page needs, as pure functions with no React and
 * no store in them — the same arrangement `orders.ts` and `markets.ts` have.
 *
 * The reason it matters here is that one of these values is a **risk
 * ceiling**, read by both order tickets. A ceiling that can be edited is a
 * number two components have to agree about, and the way they agree is by
 * calling the same function rather than each reaching into the fixture with
 * its own fallback. That is exactly what went wrong before: `?? 7` in one
 * ticket and `?? 0` in the other.
 *
 * Nothing here *enforces* anything. CLAUDE.md rule 4 is unambiguous — the UI
 * displays limits and the engine enforces them, and a value that arrived
 * from the client is never trusted. These functions decide what to show and
 * what to write to the audit log; `RiskManager.approve()` decides what is
 * allowed.
 */

export type NotificationChannel = 'bell' | 'discord'

// ---------------------------------------------------------------------- //
// Risk limits
// ---------------------------------------------------------------------- //

/** The configured ceiling for a key, or **null** when the key is absent.
 *
 * Null rather than a fallback number, deliberately. The two call sites this
 * replaces disagreed about what to substitute — one invented a 7% ceiling
 * that had never been configured, the other reported 0% and made every trade
 * look over-limit. Both were guesses dressed as facts. A caller that gets
 * null has to render "no ceiling configured", which is the only honest thing
 * to say when there isn't one.
 *
 * Takes the limits as an argument rather than reading the fixture, so the
 * value follows whatever the store holds. Reading `RISK_LIMITS` directly is
 * what made `ChainOrderTicket` immune to edits for the life of the tab. */
export function riskLimitFor(limits: RiskLimit[], key: RiskLimitKey): number | null {
  const found = limits.find((l) => l.key === key)
  return found ? found.value : null
}

/** Validation for the *field*, not for the trade.
 *
 * Returns an error string to show, or null when the value is acceptable.
 * `min` and `max` come off the limit itself because a count and a percentage
 * do not share a sensible range. */
export function validateRiskLimit(limit: RiskLimit, next: number): string | null {
  if (!Number.isFinite(next)) return 'Enter a number.'
  if (next <= 0) return 'Must be greater than zero.'
  if (limit.unit === 'count' && !Number.isInteger(next)) {
    return 'Must be a whole number of positions.'
  }

  const suffix = limit.unit === '%' ? '%' : ''
  if (next < limit.min) return `Minimum is ${limit.min}${suffix}.`
  if (next > limit.max) return `Maximum is ${limit.max}${suffix}.`
  return null
}

/** Whether a change loosens the ceiling.
 *
 * Only a raise gets a confirm. Lowering a limit reduces what is at risk and
 * asking twice for it would train the habit of clicking through the dialog
 * that matters. */
export function isRaise(limit: RiskLimit, next: number): boolean {
  return next > limit.value
}

/** A percentage ceiling as money, for the confirm dialog.
 *
 * "7% → 10%" is not a consequence anyone can feel; "$1,443 → $2,062 at risk
 * per trade" is. DESIGN.md requires a destructive confirm to state the
 * consequence in concrete terms. */
export function riskDollars(equity: number, pct: number): number {
  return (equity * pct) / 100
}

// ---------------------------------------------------------------------- //
// Audit log
// ---------------------------------------------------------------------- //

/** Builds one audit row. Takes `at` so a test can pin the timestamp rather
 * than assert around the clock. */
export function auditEntry(
  category: AuditCategory,
  field: string,
  previousValue: string,
  newValue: string,
  at: Date = new Date(),
): AuditLogEntry {
  const time = at.toISOString()
  return {
    id: `audit-${field}-${time}`,
    time,
    category,
    field,
    previousValue,
    newValue,
  }
}

/** The stored field for one cell of the routing matrix.
 *
 * Encodes both halves, because "Order filled" changed is not a change —
 * which channel it changed on is the whole content of the row. */
export function notificationAuditField(
  event: NotificationEvent,
  channel: NotificationChannel,
): string {
  return `${event}.${channel}`
}

const CHANNEL_LABEL: Record<NotificationChannel, string> = {
  bell: 'Bell',
  discord: 'Discord',
}

/** Turns a stored field key back into something readable.
 *
 * Falls back to the raw key rather than to an empty cell: an audit row you
 * cannot fully interpret is still evidence that something changed, and
 * blanking it would hide that. */
export function auditFieldLabel(entry: AuditLogEntry): string {
  if (entry.category === 'risk') {
    return RISK_LIMITS.find((l) => l.key === entry.field)?.label ?? entry.field
  }

  if (entry.category === 'feed') {
    return DATA_FEEDS.find((f) => f.key === entry.field)?.label ?? entry.field
  }

  const [event, channel] = entry.field.split('.')
  const eventLabel = NOTIFICATION_EVENT_LABEL[event as NotificationEvent]
  const channelLabel = CHANNEL_LABEL[channel as NotificationChannel]
  if (!eventLabel || !channelLabel) return entry.field
  return `${eventLabel} — ${channelLabel}`
}

// ---------------------------------------------------------------------- //
// Data feeds
// ---------------------------------------------------------------------- //

export interface FeedOption {
  value: string
  label: string
  /** True when the current plan cannot serve this feed. CLAUDE.md: asking
   * for `opra` or real-time `sip` on Basic returns an **auth error**, not
   * empty data — so this has to be visible in the control rather than
   * discovered in a stack trace. */
  requiresUpgrade: boolean
}

/** Which values a feed may take on a given plan.
 *
 * The one that surprises people: **historical SIP is free on Basic.** Any
 * request whose end is more than 15 minutes old may use it, and CLAUDE.md is
 * explicit that historical equity data should never be defaulted to IEX.
 * Only the latest/snapshot endpoints and the live stream are IEX-limited. */
export function feedOptionsFor(key: FeedKey, plan: DataPlan): FeedOption[] {
  const paid = plan === 'algo_trader_plus'

  if (key === 'options') {
    return [
      { value: 'indicative', label: 'Indicative (15-min delayed)', requiresUpgrade: false },
      { value: 'opra', label: 'OPRA (real-time)', requiresUpgrade: !paid },
    ]
  }

  if (key === 'stockHistorical') {
    return [
      { value: 'sip', label: 'SIP (100% of volume)', requiresUpgrade: false },
      { value: 'iex', label: 'IEX (~2.5% of volume)', requiresUpgrade: false },
    ]
  }

  return [
    { value: 'iex', label: 'IEX (~2.5% of volume)', requiresUpgrade: false },
    { value: 'sip', label: 'SIP (real-time)', requiresUpgrade: !paid },
  ]
}

/** A consequence worth stating next to the control that causes it.
 *
 * Historical bars on IEX is the dangerous one, and it is dangerous *quietly*:
 * `min_avg_volume` in a strategy YAML is compared against whatever feed
 * produced the bars, so a 5,000,000 threshold computed from IEX is filtering
 * on a fortieth of real volume and means nothing like what the strategy
 * author wrote. Nothing in the strategy document records which feed it was
 * measured against.
 *
 * Real-time IEX gets no warning: it is the only thing the free plan serves
 * live, so warning about it would be warning about the plan on every page
 * load. */
export function feedWarning(key: FeedKey, value: string): string | null {
  if (key === 'stockHistorical' && value === 'iex') {
    return 'IEX is ~2.5% of US volume. Every min_avg_volume threshold in a strategy is measured against whatever feed produced these bars — on IEX a 5,000,000 filter is reading a fortieth of real volume. SIP is free for anything older than 15 minutes.'
  }
  return null
}

// ---------------------------------------------------------------------- //
// Notifications
// ---------------------------------------------------------------------- //

/** Events that must not end up with both channels off unnoticed.
 *
 * Not a lock — every cell in the matrix is editable. Silencing one of these
 * everywhere routes a rule-9 critical alert to nowhere, so it passes through
 * a confirm that names what stops arriving.
 *
 * `stop_loss_hit` is deliberately **not** here. A stop firing is a loss
 * doing exactly what it was told to do, and treating it as a system-level
 * emergency is the same category error CLAUDE.md warns about when it keeps
 * `bearish` and `error` apart. */
const CRITICAL_EVENTS: readonly NotificationEvent[] = [
  'order_rejected',
  'daily_loss_halt',
  'engine_error',
]

export function isCriticalEvent(event: NotificationEvent): boolean {
  return CRITICAL_EVENTS.includes(event)
}

// ---------------------------------------------------------------------- //
// Sentiment self-audit
// ---------------------------------------------------------------------- //

/** PRD.md §9 — coin-flip territory. Below this, news sentiment stops being a
 * scanner input and becomes display-only. */
export const SENTIMENT_FLOOR = 52

/** Sources that have fallen through the floor.
 *
 * **Strictly** below, so 52% exactly still passes — the PRD says "falls
 * below 52%", and an off-by-one here drops a working signal out of the
 * scanner with no visible cause.
 *
 * Either window failing is enough. A source that is coin-flip at one hour is
 * not one to feed the scanner on the strength of its one-day figure, and
 * requiring both to fail would keep a half-broken signal live. */
export function demotedSources(
  rows: SentimentAccuracy[],
  floor: number = SENTIMENT_FLOOR,
): SentimentAccuracy[] {
  return rows.filter((r) => r.accuracy1h < floor || r.accuracy1d < floor)
}
