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
