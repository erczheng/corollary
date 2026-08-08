/** Formatting helpers shared by every page. P&L always carries an explicit
 * sign — color alone fails for colorblind users, screenshots, and
 * grayscale (DESIGN.md, "Color is never the only signal"). */

export function formatUsd(value: number, opts: { signed?: boolean } = {}): string {
  const sign = opts.signed ? (value > 0 ? '+' : value < 0 ? '−' : '') : ''
  const abs = Math.abs(value)
  return `${sign}$${abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function formatPct(value: number, opts: { signed?: boolean } = {}): string {
  const sign = opts.signed ? (value > 0 ? '+' : value < 0 ? '−' : '') : ''
  const abs = Math.abs(value)
  return `${sign}${abs.toFixed(2)}%`
}

export function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

/** "spx_mean_reversion" -> "Spx Mean Reversion" — reads as a name rather
 * than a snake_case identifier. Doesn't drop any part of the name; a
 * shorter display would risk misrepresenting which strategy it is. */
export function formatStrategyName(name: string): string {
  return name
    .split('_')
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ')
}

/** Text color for a signed value — bullish/bearish per DESIGN.md, never
 * error (a loss is not a system failure). */
export function signClass(value: number): string {
  if (value > 0) return 'text-bullish'
  if (value < 0) return 'text-bearish'
  return 'text-on-surface-variant'
}

export type ConfidenceTier = 'high' | 'medium' | 'low'

/** Confidence is the backtested hit rate for the setup class (PRD.md §6.3),
 * so these cuts reuse numbers this project already treats as meaningful
 * rather than inventing new ones: 65% is the win rate the promotion gate
 * requires for auto-approval (§5.3), and 50% is a coin flip. */
export function confidenceTier(confidence: number): ConfidenceTier {
  if (confidence >= 65) return 'high'
  if (confidence >= 50) return 'medium'
  return 'low'
}

/** Deliberately NOT bullish/bearish. A high-confidence *bearish* trade is
 * an ordinary thing here, and a green "71%" next to a put debit spread
 * would read as direction rather than conviction — the same category of
 * error as collapsing `bearish` into `error`. DESIGN.md already assigns
 * `primary` to "high-confidence indicators" and `caution` to "medium
 * confidence"; teal / terracotta / grey carry no directional meaning.
 *
 * These are container pairs, not bare tokens, because bare `neutral`
 * (#717879) is only 4.27:1 on `surface` — the muted-grey trap DESIGN.md
 * calls out under Accessibility. Every pair below clears 4.5:1 in both
 * themes (lowest is 4.55:1). */
export const CONFIDENCE_TIER_CLASS: Record<ConfidenceTier, string> = {
  high: 'bg-primary-container text-on-primary-container',
  medium: 'bg-caution-container text-on-caution-container',
  low: 'bg-neutral-container text-on-neutral-container',
}

const ET_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: 'numeric',
  minute: '2-digit',
})

const ET_DATE_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
})

const ET_DATE = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** All timestamps are stored UTC, displayed in America/New_York — market
 * data is Eastern, never local. See CLAUDE.md conventions. */
export function formatTimeET(iso: string): string {
  return ET_TIME.format(new Date(iso))
}

export function formatDateTimeET(iso: string): string {
  return ET_DATE_TIME.format(new Date(iso))
}

export function formatDateET(iso: string): string {
  return ET_DATE.format(new Date(iso))
}

const EXPIRY = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
})

/** A contract expiry is a calendar date, not an instant, so it is the one
 * thing here that is NOT rendered in America/New_York. A bare
 * 'YYYY-MM-DD' parses as UTC midnight, and ET is behind UTC, so
 * formatting it in ET renders the *previous day* — a Nov 21 expiry
 * displays as Nov 20. Verified; format date-only values in UTC.
 *
 * The year is omitted deliberately: a recommended contract is always
 * forward-dated, so "Jan 16" seen in August can only mean next January. */
export function formatExpiry(isoDate: string): string {
  return EXPIRY.format(new Date(`${isoDate}T00:00:00Z`))
}

const DATE_ONLY = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
})

/** A calendar date with its year — chart axis dates, a series start date.
 * Same rule as formatExpiry and for the same reason: a bare 'YYYY-MM-DD'
 * parses as UTC midnight, so rendering it in ET shows the previous day.
 * Use this for any date-only value, not formatDateET, which is for
 * instants. */
export function formatDateOnly(isoDate: string): string {
  return DATE_ONLY.format(new Date(`${isoDate}T00:00:00Z`))
}
