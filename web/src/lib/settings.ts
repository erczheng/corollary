import { DATA_FEEDS, RISK_LIMITS } from './mockData'
import { isApiError, isUnreachable } from './api'
import { NEWS_SOURCE_LABEL } from './news'
import {
  NOTIFICATION_EVENT_LABEL,
  type AuditCategory,
  type AuditLogEntry,
  type DataFeed,
  type DataPlan,
  type FeedKey,
  type NewsLabelSource,
  type NotificationEvent,
  type RiskLimit,
  type RiskLimitKey,
  type SentimentAuditStatus,
  type SentimentCounts,
  type SentimentSourceAudit,
  type SentimentWindow,
  type SentimentWindowAccuracy,
} from './types'

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
 * that matters.
 *
 * **A null previous value is not a raise.** There is no configured ceiling to
 * raise *from*, so a confirm would have to quote a previous figure nobody
 * set — the `?? 7` mistake wearing a dialog. Storing the first ceiling on a
 * limit that had none is the constraining direction anyway, and the server
 * audits it either way. */
export function isRaise(limit: RiskLimit, next: number): boolean {
  if (limit.value === null) return false
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

/** What a row's label can be resolved against.
 *
 * Passed in rather than read from the fixture so the log follows whatever the
 * server served, the same reason `riskLimitFor` takes its limits. Defaults to
 * the fixtures, which carry the identical labels — the server mirrors them —
 * so a caller with nothing loaded yet still gets words rather than keys. */
export interface AuditCatalog {
  limits?: RiskLimit[]
  feeds?: DataFeed[]
}

/** Turns a stored field key back into something readable.
 *
 * Falls back to the raw key rather than to an empty cell: an audit row you
 * cannot fully interpret is still evidence that something changed, and
 * blanking it would hide that.
 *
 * **A feed row is keyed by its env var, not by its camelCase key.** The
 * server writes `field = ALPACA_STOCK_FEED_HISTORICAL` (`settings.py`), while
 * the feed it describes is `stockHistorical`, so matching on `key` alone
 * printed the raw variable name in a column that promises readable settings.
 * Both are accepted here because both are true names for the same row. */
export function auditFieldLabel(entry: AuditLogEntry, catalog: AuditCatalog = {}): string {
  if (entry.category === 'risk') {
    const limits = catalog.limits ?? RISK_LIMITS
    return limits.find((l) => l.key === entry.field)?.label ?? entry.field
  }

  if (entry.category === 'feed') {
    const feeds = catalog.feeds ?? DATA_FEEDS
    const feed = feeds.find((f) => f.key === entry.field || f.envVar === entry.field)
    return feed?.label ?? entry.field
  }

  // A watchlist row is keyed by the ticker watched or unwatched (decision 21:
  // the unit of change is one ticker). It needs its own branch — falling
  // through to the notification split below returned the bare ticker with
  // nothing to say it was a watch. *Assumption*: the backend route that
  // writes these rows was not yet on disk when this was written; if it keys
  // them otherwise, the label still carries the raw field, never a blank.
  if (entry.category === 'watchlist') {
    return `News watchlist — ${entry.field}`
  }

  // A status transition is keyed by the source it moved (decision 13). An
  // unknown source keeps its raw key, never a blank.
  if (entry.category === 'sentiment') {
    const source = NEWS_SOURCE_LABEL[entry.field as NewsLabelSource] ?? entry.field
    return `Sentiment source — ${source}`
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

//
// The status is the server's. Nothing below decides whether a source is
// demoted: `sentiment_source_status` is written by the audit run and nothing
// else, and a client-side recomputation from accuracy is how this page and
// the engine would come to disagree about which labels the scanner reads.
// These helpers derive *display* from counts the server sent.

/** Decision 13 — below this percentage in **either** window demotes a source
 * (at the sample floor). Mirrors `DEMOTE_BELOW_PCT` in
 * `corollary/data/news/audit.py`; `settings.test.ts` reads that file and
 * fails if the two part. Display only: the engine applies it. */
export const SENTIMENT_FLOOR = 52

/** Decision 13 — at or above this percentage in **both** windows re-promotes a
 * demoted source. Mirrors `REPROMOTE_AT_PCT` in `audit.py`, pinned the same
 * way. Between the two thresholds is a dead band: a source there stays where
 * it is, whichever side it came from. */
export const SENTIMENT_REPROMOTE = 66

/** Graded directional labels needed in each window, over the trailing 28
 * days, before a source may be demoted. Mirrors `DEMOTION_FLOOR`. The
 * per-source figure the page compares against is `eligibility.floor`, which
 * the server sends; this is for the sentence that states the rule. */
export const SENTIMENT_DEMOTE_SAMPLE = 30

/** Graded directional labels needed in each window since the demotion before
 * a source may be re-promoted. Mirrors `REPROMOTION_FLOOR`. */
export const SENTIMENT_REPROMOTE_SAMPLE = 100

/** The two scoring windows, in the order the table shows them. */
export const SENTIMENT_WINDOWS: readonly SentimentWindow[] = ['1h', '1d']

export const SENTIMENT_WINDOW_LABEL: Record<SentimentWindow, string> = {
  '1h': '1 hour',
  '1d': '1 day',
}

/** Accuracy as an integer count of **tenths of a percent**, truncated —
 * `correct / graded`, with neutral labels in neither (Q5). `null` with
 * nothing graded: 0 of 0 is not 0%.
 *
 * Integers end to end, the way `audit.py`'s `_below` and `_pct` work, so the
 * figure on screen can never round across a threshold the engine compared
 * exactly: 51.96% shows as 51.9%, not 52.0%. Format with `formatTenthsPct`. */
export function accuracyTenths(counts: SentimentCounts): number | null {
  if (counts.graded <= 0) return null
  return Math.floor((counts.correct * 1000) / counts.graded)
}

/** Below the sample floor, a percentage is not yet a measurement — the cell
 * says how far short it is instead. */
export function isInsufficientSample(counts: SentimentCounts, floor: number): boolean {
  return counts.graded < floor
}

/** The cell a source is judged on in one window: the basis its
 * `eligibility` names (`trailing_28d`, or `since_demotion` once demoted).
 * `null` when the latest run wrote no row on that basis — which reads as
 * nothing graded yet, the same zero the server's eligibility reports. */
export function actingCell(
  source: SentimentSourceAudit,
  window: SentimentWindow,
): SentimentWindowAccuracy | null {
  return (
    source.accuracy.find((a) => a.window === window && a.basis === source.eligibility.basis) ??
    null
  )
}

/** The counts a cell shows; zeros for a window with no row on the basis. */
export function cellCounts(cell: SentimentWindowAccuracy | null): SentimentCounts {
  return cell?.combined ?? { graded: 0, correct: 0, neutral: 0 }
}

/** The sources the banner names: those whose **server** status is
 * `demoted`. A missing status row is a display fallback (`unaudited`), never
 * a demotion, and is reported separately. */
export function demotedSources(sources: readonly SentimentSourceAudit[]): SentimentSourceAudit[] {
  return sources.filter((s) => !s.statusMissing && s.status === 'demoted')
}

/** What the status cell says a source is. `missing` comes first: with no
 * status row the served `status` and `neverAudited` are a fallback, and
 * calling that "never audited" would state a fact nobody recorded. */
export type SourceStatusKind = 'missing' | 'unaudited' | 'active' | 'demoted'

export function sourceStatusKind(source: SentimentSourceAudit): SourceStatusKind {
  if (source.statusMissing) return 'missing'
  if (source.neverAudited || source.status === 'unaudited') return 'unaudited'
  return source.status
}

/** A demoted source's road back, one sentence per window:
 * "since demotion: 41 of 100 in the 1h window". Empty for any source the
 * server is not counting toward re-promotion. */
export function repromotionProgress(source: SentimentSourceAudit): string[] {
  if (source.eligibility.kind !== 'repromote') return []
  return source.eligibility.windows.map(
    (w) => `since demotion: ${w.graded} of ${source.eligibility.floor} in the ${w.window} window`,
  )
}

/** Whether every source is waiting on its first run — the readout's empty
 * state, which says when the first one is due rather than drawing a table of
 * zeros as though they were results. */
export function nothingAuditedYet(status: SentimentAuditStatus): boolean {
  return status.accuracyRun === null
}

/** A refused `POST /api/settings/sentiment/audit`, sorted into what the page
 * says about it.
 *
 * - `rate_limited` (429) — when the button works again: `Retry-After` from
 *   the refusal itself when it carried one, counted from when the request was
 *   sent, else the readout's `manualAvailableAt`. A fact, not a fault.
 * - `running` (409) — a run is open; the server's sentence names which.
 * - `unavailable` (503) — a data dependency the run needs is absent or
 *   degraded, so nothing was started. Rendered `caution`, not `error`: it is
 *   the "unvalidated, pending" state DESIGN.md gives `caution`, and nothing
 *   broke.
 * - `failed` (500, unreachable, anything else) — a real fault, `error`.
 *
 * The server's own message is kept every time: its 503 and 500 texts are
 * fixed sentences written for exactly this screen. */
export type AuditTriggerRefusal =
  | { kind: 'rate_limited'; availableAt: string | null; message: string }
  | { kind: 'running'; message: string }
  | { kind: 'unavailable'; message: string }
  | { kind: 'failed'; message: string }

export function auditTriggerRefusal(
  error: unknown,
  context: { requestedAt: number | null; manualAvailableAt: string | null },
): AuditTriggerRefusal {
  if (isUnreachable(error)) {
    return {
      kind: 'failed',
      message: 'The engine did not answer. Check that it is running, then try again.',
    }
  }
  if (!isApiError(error)) {
    return { kind: 'failed', message: 'The audit could not be started, and no reason was given.' }
  }
  if (error.status === 429) {
    const fromHeader =
      error.retryAfterSeconds !== null && context.requestedAt !== null
        ? new Date(context.requestedAt + error.retryAfterSeconds * 1000).toISOString()
        : null
    return {
      kind: 'rate_limited',
      availableAt: fromHeader ?? context.manualAvailableAt,
      message: error.message,
    }
  }
  if (error.status === 409) return { kind: 'running', message: error.message }
  if (error.status === 503) return { kind: 'unavailable', message: error.message }
  return { kind: 'failed', message: error.message }
}
