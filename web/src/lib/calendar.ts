/**
 * The News page's market calendar, as pure rules (Phase 3 step 7). No React
 * in here, so every one of them is testable without rendering the panel —
 * the split `orders.ts` and `markets.ts` use.
 *
 * **The served `date` is the grouping key, never a date derived from `at`.**
 * `2026-10-15T00:30:00Z` is 8:30pm ET on **October 14**, so an event filed
 * by its UTC instant lands on the next day. The server groups by Eastern
 * date and this module never regroups.
 *
 * **Figures are decimal strings and stay strings.** An EPS estimate of
 * `1.2350` is a reported number whose digits are the fact; parsed to a
 * float it prints `1.235`, and a CPI print loses its trailing zero.
 */

import { formatSessionDay } from './format'
import type {
  CalendarEvent,
  CalendarEventType,
  CalendarJobName,
  CalendarJobNotice,
  CalendarJobState,
  CalendarRange,
  CalendarSource,
  EarningsSession,
  IpoStatus,
} from './types'

/** How far ahead the panel asks. Four weeks: past the earnings job's
 * 21-day horizon on purpose, so that the dates it has *not* asked about are
 * visible and said aloud rather than silently cut off. The server allows 92. */
export const CALENDAR_WINDOW_DAYS = 28

/** `YYYY-MM-DD` plus `n` calendar days, in UTC so no offset can move it. */
export function addDays(date: string, n: number): string {
  const ms = Date.parse(`${date}T00:00:00Z`) + n * 86_400_000
  return new Date(ms).toISOString().slice(0, 10)
}

/** The range the panel requests: Eastern today, inclusive, forward. */
export function calendarWindow(
  today: string,
  days: number = CALENDAR_WINDOW_DAYS,
): { from: string; to: string } {
  return { from: today, to: addDays(today, days - 1) }
}

/* ---------------------------------------------------------------------- *
 * Within a day
 * ---------------------------------------------------------------------- */

/** The order event types read best in within a day. */
export const TYPE_ORDER: readonly CalendarEventType[] = [
  'economic',
  'central-bank',
  'earnings',
  'ipo',
  'dividend',
  'geopolitical',
]

/** A day's events grouped by type, in {@link TYPE_ORDER}, each group in the
 * server's order. The type is printed once per group rather than on every
 * row, which is what lets a day fit in a card. Does not mutate. */
export function groupByType(
  events: readonly CalendarEvent[],
): [CalendarEventType, CalendarEvent[]][] {
  return TYPE_ORDER.map(
    (type) => [type, events.filter((e) => e.type === type)] as [CalendarEventType, CalendarEvent[]],
  ).filter(([, group]) => group.length > 0)
}

export const SESSION_LABEL: Record<EarningsSession, string> = {
  bmo: 'Before open',
  amc: 'After close',
  dmh: 'During market hours',
}

export const IPO_STATUS_LABEL: Record<IpoStatus, string> = {
  expected: 'Expected',
  filed: 'Filed',
  priced: 'Priced',
  withdrawn: 'Withdrawn',
}

/** The banks that may publish a date with no time, by the title prefix the
 * seed writes (`"BoJ policy decision"`). FOMC always carries 14:00 ET. */
const LOCAL_DATE_BANKS: readonly { prefix: string; city: string; bank: string }[] = [
  { prefix: 'BoJ', city: 'Tokyo', bank: 'The Bank of Japan' },
  { prefix: 'BoE', city: 'London', bank: 'The Bank of England' },
  { prefix: 'ECB', city: 'Frankfurt', bank: 'The ECB' },
]

/** What sits in a row's time slot. Exactly one of these, by rule:
 *
 * - **earnings** always reads its session, never a time (decision 7) — even
 *   if an instant were present, a vendor's `hour` is a session;
 * - a **date-only central-bank** row reads as the bank's own local date,
 *   with the reason, so a New York reader is not misled by a day;
 * - any other **timed** row reads its time in ET;
 * - any other date-only row reads "All day" — never a placeholder midnight.
 */
export type TimeSlot =
  | { kind: 'session'; text: string }
  | { kind: 'time'; at: string }
  | { kind: 'local-date'; text: string; detail: string }
  | { kind: 'all-day'; text: string }

export function timeSlot(event: CalendarEvent): TimeSlot {
  if (event.type === 'earnings') {
    return {
      kind: 'session',
      text: event.session ? SESSION_LABEL[event.session] : 'Session not given',
    }
  }
  if (event.at !== null) return { kind: 'time', at: event.at }
  if (event.type === 'central-bank') {
    const bank = LOCAL_DATE_BANKS.find((b) => event.title.startsWith(b.prefix))
    if (bank) {
      return {
        kind: 'local-date',
        text: `${bank.city} date`,
        detail: `${bank.bank} publishes no announcement time, so this is the decision's date in ${bank.city}, not a New York time. It can fall on the previous evening or the following morning in New York.`,
      }
    }
  }
  return { kind: 'all-day', text: 'All day' }
}

/* ---------------------------------------------------------------------- *
 * Figures
 * ---------------------------------------------------------------------- */

/** An economic row's prior/actual, said in words when they are absent.
 *
 * `pending` is the owner-decision state (unit 7.2b-R): which FRED series is
 * a release's headline figure is unanswered, so both are null on every
 * economic row and the panel says *why* rather than leaving blanks. */
export type EconomicFigures =
  | { kind: 'pending' }
  | { kind: 'figures'; prior: string | null; actual: string | null; unit: string | null }

export function economicFigures(
  event: CalendarEvent,
  range: Pick<CalendarRange, 'notices'>,
): EconomicFigures {
  const none = event.prior === null && event.actual === null
  if (none && range.notices.releaseFigures.state === 'pending_owner_decision') {
    return { kind: 'pending' }
  }
  return { kind: 'figures', prior: event.prior, actual: event.actual, unit: event.unit }
}

/* ---------------------------------------------------------------------- *
 * Notices
 * ---------------------------------------------------------------------- */

export const JOB_LABEL: Record<CalendarJobName, string> = {
  calendar_earnings: 'Earnings',
  calendar_ipo: 'IPOs',
  calendar_dividends: 'Dividends',
  calendar_releases: 'Economic releases',
  calendar_central_banks: 'Central banks',
}

/** A short status for each job state. `access_denied` is a premium refusal
 * — "not on this plan", not "try again" (Q15) — and `failing` is staleness,
 * not emptiness. `fresh_at_start` is calm: the rows were written by an
 * earlier process less than a day ago. */
export const JOB_STATE_LABEL: Record<CalendarJobState, string> = {
  scheduler_not_running: 'No scheduler',
  not_scheduled: 'Not scheduled',
  never_run: 'Not fetched yet',
  ok: 'Up to date',
  fresh_at_start: 'Up to date',
  skipped: 'Skipped',
  failing: 'Stale',
  access_denied: 'Not on this plan',
}

/** How loudly a state is said. `failing` is a system condition and renders
 * in `error`; `access_denied` is a standing refusal the owner has to decide
 * on, `caution`; the rest are calm. */
export type JobTone = 'error' | 'caution' | 'calm'

export function jobTone(state: CalendarJobState): JobTone {
  if (state === 'failing') return 'error'
  if (state === 'access_denied') return 'caution'
  return 'calm'
}

/** The last date a job has asked its vendor about, when that is short of the
 * range's end; `'all'` when nothing is known to have been fetched (short of
 * everything); `null` when the job covers the whole range. Check `'all'`
 * before treating the result as a date — it is a string too. */
export function uncoveredAfter(job: CalendarJobNotice, end: string): string | null | 'all' {
  if (job.coveredThrough === null) return 'all'
  return job.coveredThrough < end ? job.coveredThrough : null
}

/** What an **empty** range means, which is three different things:
 *
 * - `failing` — a feed is failing or refused; the emptiness is not a fact
 *   about the market;
 * - `incomplete` — a feed has not run, was skipped, or does not reach the
 *   whole range, or a central-bank seed has a gap; "nothing scheduled" would
 *   be a claim about dates nobody asked about;
 * - `quiet` — every feed ran and covers the range, and found nothing.
 *
 * Only `quiet` may say "nothing scheduled". An empty panel at 08:00 because
 * the 07:00 fetch has not happened in this process reads as `incomplete`,
 * and a feed that broke reads as `failing`. */
export type EmptyReading = 'failing' | 'incomplete' | 'quiet'

export function emptyReading(range: CalendarRange): EmptyReading {
  const jobs = range.notices.jobs
  if (jobs.some((j) => j.state === 'failing' || j.state === 'access_denied')) return 'failing'
  const settled = (s: CalendarJobState) => s === 'ok' || s === 'fresh_at_start'
  if (
    range.notices.seedGaps.length > 0 ||
    jobs.length === 0 ||
    jobs.some((j) => !settled(j.state) || uncoveredAfter(j, range.end) !== null)
  ) {
    return 'incomplete'
  }
  return 'quiet'
}

/** Where a row came from, in a sentence-ready phrase for the 409. */
export const SOURCE_PHRASE: Record<CalendarSource, string> = {
  finnhub: 'Finnhub',
  alpaca: 'Alpaca',
  fred: 'FRED',
  seed: 'the central-bank seed',
  manual: 'a manual entry',
}

/** The remove confirm's consequence, concrete: which row, which day. */
export function removeConsequence(event: Pick<CalendarEvent, 'title' | 'date'>): string {
  return `Remove '${event.title}' on ${formatSessionDay(event.date)} from the calendar? It disappears from every range on this page. The row is kept server-side as removed and cannot be restored from here — adding it back means typing it again.`
}

/* ---------------------------------------------------------------------- *
 * The manual form's time field — Eastern wall clock <-> an ISO instant
 * ---------------------------------------------------------------------- */

const ET_PARTS = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hourCycle: 'h23',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

/** ET's offset from UTC at an instant, in minutes (−240 in summer). */
function etOffsetMinutes(ms: number): number {
  const parts = Object.fromEntries(
    ET_PARTS.formatToParts(new Date(ms)).map((p) => [p.type, p.value]),
  ) as Record<string, string>
  const wall = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour),
    Number(parts.minute),
    Number(parts.second),
  )
  return Math.round((wall - ms) / 60_000)
}

/** An Eastern wall-clock time on an Eastern date, as an ISO instant **with
 * its offset** (`2026-10-16T09:30:00-04:00`) — the server refuses an
 * instant without one. The offset is the one in force at that instant, so
 * a November date after the clocks change gets `-05:00`.
 *
 * Returns null for malformed input; the server is the validator, this only
 * declines to build a string from nonsense. */
export function etInstant(date: string, hhmm: string): string | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  const t = /^(\d{2}):(\d{2})$/.exec(hhmm)
  if (!d || !t) return null
  const naive = Date.UTC(Number(d[1]), Number(d[2]) - 1, Number(d[3]), Number(t[1]), Number(t[2]))
  // Two passes: the offset at the naive guess can differ from the one at
  // the true instant when the guess straddles a DST change.
  let offset = etOffsetMinutes(naive - etOffsetMinutes(naive) * 60_000)
  offset = etOffsetMinutes(naive - offset * 60_000)
  const sign = offset < 0 ? '-' : '+'
  const abs = Math.abs(offset)
  const hh = String(Math.floor(abs / 60)).padStart(2, '0')
  const mm = String(abs % 60).padStart(2, '0')
  return `${date}T${hhmm}:00${sign}${hh}:${mm}`
}

/** An instant's Eastern wall-clock time as `HH:MM`, for a time input. */
export function etClock(iso: string): string {
  const parts = Object.fromEntries(
    ET_PARTS.formatToParts(new Date(iso)).map((p) => [p.type, p.value]),
  ) as Record<string, string>
  return `${parts.hour}:${parts.minute}`
}
