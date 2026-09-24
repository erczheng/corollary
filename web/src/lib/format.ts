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

/** A signed plain number — no currency, no percent.
 *
 * For quantities measured in their own units, where a `$` would be wrong and
 * a `%` would be a different number: the VIX moves in points. Uses the same
 * U+2212 minus as the money and percent formatters, so a column of signed
 * values lines up whatever produced it. */
export function formatSignedNumber(value: number, fractionDigits = 2): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}${Math.abs(value).toFixed(fractionDigits)}`
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

const ET_SESSION_DATE_TIME = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
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

/** An instant with its weekday, its date, its year *and* its clock time —
 * "Fri, Sep 11, 2026, 3:55 PM".
 *
 * The long form exists for a chart whose x-axis no longer encodes elapsed
 * time. An intraday range spanning several sessions is drawn on an **ordinal**
 * axis so Friday 16:00 butts against Monday 09:30 with no overnight dead
 * space, and once the axis is a position rather than a clock the crosshair is
 * the only thing left that can answer "when was that spike". `formatTimeET`
 * cannot ("3:55 PM" on which of five days) and `formatDateTimeET` is short a
 * weekday, which is the part that makes a concatenated week readable.
 *
 * An **instant**, in ET like every other timestamp here — not a date-only
 * value. `IntradayPoint.at` is UTC, stamped at the interval's open, and
 * carries its own offset, so it goes straight to `new Date`. Compare
 * `formatSessionDay`, which takes a bare `YYYY-MM-DD` and must format in UTC
 * for the opposite reason. */
export function formatSessionDateTimeET(iso: string): string {
  return ET_SESSION_DATE_TIME.format(new Date(iso))
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

const SESSION_DAY = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
})

/** A trading session's day, named — "Fri, Sep 11".
 *
 * Same UTC rule as formatDateOnly and formatExpiry, and for the same
 * reason: the argument is a bare 'YYYY-MM-DD', which is UTC midnight, so
 * rendering it in ET would name the *previous* day and label a Friday
 * session Thursday. An **instant** must be resolved to its market date
 * first (api.ts#latestSession) rather than handed straight to this.
 *
 * The weekday is the point of the format. "Which day was the last market
 * day" is a question about the weekend or the holiday sitting between then
 * and now, and "Fri" answers it without arithmetic. The year is omitted for
 * the same reason formatExpiry omits it — the newest point of a live chart
 * is always recent. */
export function formatSessionDay(isoDate: string): string {
  return SESSION_DAY.format(new Date(`${isoDate}T00:00:00Z`))
}

/** Today's calendar date in market time, as 'YYYY-MM-DD'.
 *
 * `en-CA` because it is the locale whose short date *is* ISO order; the
 * time zone is the point of the function. Every date-only comparison in the
 * app parses as UTC midnight, and this returns a string of exactly that
 * shape so it can be one side of that comparison.
 *
 * **Why not `new Date().toISOString().slice(0, 10)`** — that is today in
 * UTC, which after 20:00 ET is tomorrow. A DTE computed against it reads
 * one day short every evening, and "1 DTE" on a contract expiring tomorrow
 * is the kind of wrong that gets acted on.
 *
 * Fixtures use `MARKET_TODAY` instead, which is a fixed date so they do not
 * rot. Live positions use this: a real expiry is measured against the real
 * day, and `MARKET_TODAY` drifts a day further from it every morning. */
const MARKET_DAY = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

export function marketToday(now: Date = new Date()): string {
  return MARKET_DAY.format(now)
}

/** Grouped integer — share counts, contract volume, open interest. Not
 * compact: a chain is scanned for exact size, and "37.5K" loses the digit
 * that distinguishes a busy strike from a very busy one. */
export function formatInteger(value: number): string {
  return value.toLocaleString('en-US')
}

/** Market capitalisation, carried in billions.
 *
 * `null` is an ETF, which has no market capitalisation at all — it renders
 * an em dash rather than $0.00B, which would read as a fund worth nothing
 * and would sort below every real company. The unit is part of the value
 * here, so it is not left to the column header. */
export function formatMarketCap(dollars: number | null): string {
  // Dollars, as the wire carries them: the server scales Finnhub's millions
  // with an exact Decimal. This took billions while the column was a fixture,
  // and read a live $5.43T as "$5434790884.05T".
  if (dollars === null) return '—'
  if (dollars >= 1e12) return `$${(dollars / 1e12).toFixed(2)}T`
  if (dollars >= 1e9) return `$${(dollars / 1e9).toFixed(2)}B`
  if (dollars >= 1e6) return `$${(dollars / 1e6).toFixed(2)}M`
  return `$${dollars.toLocaleString('en-US')}`
}

/** Implied volatility, stored as a decimal and read as a percentage. One
 * decimal place: the chain's smirk moves in tenths, and rounding to whole
 * points flattens adjacent strikes into a tie. */
export function formatIv(iv: number): string {
  return `${(iv * 100).toFixed(1)}%`
}
