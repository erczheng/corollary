/**
 * Filtering, ranking and derivation for the News page (PRD.md §8.3). Pure
 * functions with no React in them, so the rules can be tested without
 * rendering a feed — the same split `markets.ts` and `orders.ts` use.
 *
 * None of these mutate their input. The feed is one array that every filter
 * reads, so an in-place `.sort()` would leave the last view's ordering
 * behind in the data itself.
 */

import {
  MACRO_SECTOR,
  type CalendarEvent,
  type DiscoveryEvaluation,
  type DiscoveryFeedName,
  type NewsItem,
  type NewsLabelSource,
  type NewsLookback,
  type NewsMover,
  type NewsMovers,
  type NewsMoverReason,
  type NewsSourceStatus,
  type SectorConsensus,
  type SentimentComponent,
  type SocialAttentionItem,
} from './types'
import { formatDateTimeET, formatSessionDateTimeET, formatTimeET } from './format'

// ---------------------------------------------------------------------- //
// Market sentiment composite
// ---------------------------------------------------------------------- //

/** The 0-100 composite, as the equally-weighted mean of its components.
 *
 * **Derived, never stored.** PRD.md §8.3 specifies seven components, each
 * z-scored on a trailing window and equally weighted, with the breakdown
 * visible on hover — which means the headline number and the seven rows
 * under it are two renderings of one fact. A stored composite is the
 * version of this that can drift, and a gauge reading 58 above components
 * averaging 61 discredits both figures at once. Same rule `activityStats`
 * follows on Activity: compute the header from the rows beneath it. */
export function compositeScore(components: SentimentComponent[]): number {
  if (components.length === 0) return 0
  return Math.round(components.reduce((sum, c) => sum + c.score, 0) / components.length)
}

/** CNN's five published bands, which the composite mirrors.
 *
 * Kept as bands rather than a raw number because 58 does not mean anything
 * on its own — the whole point of the index is the word it maps to. */
export type SentimentBand = 'extreme-fear' | 'fear' | 'neutral' | 'greed' | 'extreme-greed'

export const SENTIMENT_BAND_LABEL: Record<SentimentBand, string> = {
  'extreme-fear': 'Extreme fear',
  fear: 'Fear',
  neutral: 'Neutral',
  greed: 'Greed',
  'extreme-greed': 'Extreme greed',
}

export function sentimentBand(score: number): SentimentBand {
  if (score < 25) return 'extreme-fear'
  if (score < 45) return 'fear'
  if (score <= 55) return 'neutral'
  if (score <= 75) return 'greed'
  return 'extreme-greed'
}

/** Which semantic family a band renders in.
 *
 * `bullish`/`bearish`, and this is the one place on this page that is
 * *correct* rather than a slip: the composite is a directional read on the
 * tape, so 20 genuinely means the market is fearful and 80 genuinely means
 * it is greedy. That is a different question from confidence, which
 * CLAUDE.md correctly forbids from using these tokens — a high-confidence
 * bearish trade is ordinary, but there is no such thing as a fearful
 * reading that is secretly bullish.
 *
 * Never `error`. Extreme fear is a market condition, not a system failure,
 * and the split between `bearish` and `error` holds here exactly as it does
 * on a losing position. */
export type SentimentTone = 'bullish' | 'bearish' | 'neutral'

export const SENTIMENT_BAND_TONE: Record<SentimentBand, SentimentTone> = {
  'extreme-fear': 'bearish',
  fear: 'bearish',
  neutral: 'neutral',
  greed: 'bullish',
  'extreme-greed': 'bullish',
}

export function sentimentTone(score: number): SentimentTone {
  return SENTIMENT_BAND_TONE[sentimentBand(score)]
}

// ---------------------------------------------------------------------- //
// The live feed
// ---------------------------------------------------------------------- //

/** How far back the feed reaches. `all` is the whole corpus, which is the
 * "historical lookback" PRD.md §8.3 asks for — the other steps are the
 * common questions ("what happened today", "what happened this week") that
 * would otherwise mean paging through a fortnight. */
export type Lookback = 'today' | '3d' | '1w' | '2w' | 'all'

export const LOOKBACKS: Lookback[] = ['today', '3d', '1w', '2w', 'all']

export const LOOKBACK_LABEL: Record<Lookback, string> = {
  today: 'Today',
  '3d': 'Last 3 days',
  '1w': 'Last week',
  '2w': 'Last 2 weeks',
  all: 'All time',
}

/** Calendar days, not sessions. A reader asking for "the last 3 days" on a
 * Monday means the weekend is included and empty, not that they want the
 * previous Wednesday. `null` is no bound at all. */
const LOOKBACK_DAYS: Record<Lookback, number | null> = {
  today: 1,
  '3d': 3,
  '1w': 7,
  '2w': 14,
  all: null,
}

export interface NewsFilter {
  /** A sector, or `null` for every sector. */
  sector: string | null
  publisher: string | null
  /** A sentiment label, or `null` for every label. Includes
   * `unclassified`, which is a real answer to "show me what the LLM would
   * not commit to" and not an absence to be filtered away silently. */
  sentiment: NewsItem['sentiment'] | null
  lookback: Lookback
}

export const DEFAULT_NEWS_FILTER: NewsFilter = {
  sector: null,
  publisher: null,
  sentiment: null,
  lookback: 'all',
}

/** The distinct sectors present in the feed, macro last.
 *
 * Derived from the corpus rather than listed, so the dropdown can never
 * offer a sector that returns nothing. Macro sorts to the end because it is
 * not a GICS sector — it is the bucket for stories about no single name,
 * and alphabetising it between Health Care and Technology implies a
 * peerage it does not have. */
export function newsSectors(items: NewsItem[]): string[] {
  return orderSectors(items.map((i) => i.sector))
}

/** Sector names for a dropdown: distinct, alphabetical, Macro last — the
 * ordering `newsSectors` documents, over bare names. The live feed needs it
 * over names it has *seen* across pages, because `GET /api/news` serves a
 * page, not the list of sectors a filter could take. */
export function orderSectors(names: Iterable<string>): string[] {
  const seen = [...new Set(names)]
  const sectors = seen.filter((s) => s !== MACRO_SECTOR).sort((a, b) => a.localeCompare(b))
  return seen.includes(MACRO_SECTOR) ? [...sectors, MACRO_SECTOR] : sectors
}

/** The lookback as it reads inside a sentence — "No headlines on your watch
 * list {phrase}". `all` reads "yet": with no bound, empty means nothing has
 * been ingested, not that the window was quiet. */
export const LOOKBACK_PHRASE: Record<Lookback, string> = {
  today: 'today',
  '3d': 'in the last 3 days',
  '1w': 'in the last week',
  '2w': 'in the last 2 weeks',
  all: 'yet',
}

export function newsPublishers(items: NewsItem[]): string[] {
  // A null publisher is "the vendor named none" — not a publisher, so not an
  // option to filter by.
  const named = items.map((i) => i.publisher).filter((p): p is string => p !== null)
  return [...new Set(named)].sort((a, b) => a.localeCompare(b))
}

/** Every filter narrows the same set — they **combine** rather than
 * replacing each other, the same rule Activity's search and status filter
 * follow. "Bearish Technology headlines from Reuters this week" is the
 * question a hundred-row feed raises, and any one of these alone cannot
 * answer it.
 *
 * `now` is passed in rather than read from the clock so the lookback is
 * testable and so the caller decides which clock applies — the feed runs on
 * the fixture's, not the wall's. */
export function filterNews(items: NewsItem[], filter: NewsFilter, now: string): NewsItem[] {
  const days = LOOKBACK_DAYS[filter.lookback]
  // The earliest ET session still in the window, compared as a date string.
  //
  // Deliberately not an epoch cutoff computed from a fixed `-04:00`: that
  // offset is EDT, and every headline from November to March would land an
  // hour out of place. Comparing `YYYY-MM-DD` sidesteps the offset
  // entirely, and it is the comparison the reader is making anyway —
  // "today" means today's session, not the trailing 24 hours, which at
  // 9:05am would quietly include most of yesterday.
  const cutoff = days === null ? null : shiftDate(etDate(now), -(days - 1))

  return items.filter((i) => {
    if (filter.sector !== null && i.sector !== filter.sector) return false
    if (filter.publisher !== null && i.publisher !== filter.publisher) return false
    if (filter.sentiment !== null && i.sentiment !== filter.sentiment) return false
    if (cutoff !== null && etDate(i.time) < cutoff) return false
    return true
  })
}

/** Adds days to a `YYYY-MM-DD`, staying a date throughout.
 *
 * Parsed as UTC midnight and formatted back from UTC, so this is pure
 * calendar arithmetic with no timezone in it — the trap `formatExpiry`
 * documents, avoided by never letting the value become a local instant. */
function shiftDate(date: string, days: number): string {
  const d = new Date(`${date}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/** The Eastern calendar date an instant falls in, as `YYYY-MM-DD`.
 *
 * Market data is Eastern and the feed's sessions are Eastern, so bucketing
 * on the UTC date puts every headline after 8pm ET on the following day.
 * `en-CA` is used purely because it formats as ISO. */
const ET_ISO_DATE = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/New_York',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

export function etDate(iso: string): string {
  return ET_ISO_DATE.format(new Date(iso))
}

export type NewsSort = 'newest' | 'oldest'

export const NEWS_SORT_LABEL: Record<NewsSort, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
}

export function sortNews(items: NewsItem[], sort: NewsSort): NewsItem[] {
  return [...items].sort((a, b) =>
    sort === 'newest' ? b.time.localeCompare(a.time) : a.time.localeCompare(b.time),
  )
}

// ---------------------------------------------------------------------- //
// Social attention
// ---------------------------------------------------------------------- //

/** This session's mentions against the name's own 30-day baseline.
 *
 * The same shape as `relativeVolume` on Markets, and for the same reason:
 * raw mention counts find the same handful of names every day, while a
 * multiple of a name's *own* baseline finds the one something is happening
 * to. A stock going from 260 mentions to 780 is the signal; SPY's 1,650 is
 * just SPY. */
export function attentionVelocity(item: SocialAttentionItem): number {
  return item.baselineMentions === 0 ? 0 : item.mentions / item.baselineMentions
}

/** The share of messages carrying a user-applied label, 0-1.
 *
 * Displayed rather than hidden because sentiment here is aggregated over
 * labelled messages *only* (PRD.md §8.3). A direction computed from 11% of
 * a small sample and one computed from 40% of a large one are not the same
 * claim, and the panel has no business presenting them identically. */
export function labeledShare(item: SocialAttentionItem): number {
  return item.sampleSize === 0 ? 0 : item.labeledCount / item.sampleSize
}

/** Most unusual first. Not most-mentioned — see `attentionVelocity`. */
export function sortAttention(items: SocialAttentionItem[]): SocialAttentionItem[] {
  return [...items].sort((a, b) => attentionVelocity(b) - attentionVelocity(a))
}

// ---------------------------------------------------------------------- //
// Sector consensus
// ---------------------------------------------------------------------- //

/** Buy share minus sell share, in percentage points.
 *
 * The single number that orders the panel. Sorting on Buy alone ranks a
 * 55/38/7 sector above a 52/40/8 one while ignoring that the second has
 * barely more sells — the net is what "top rated" actually means. */
export function consensusNet(c: SectorConsensus): number {
  return c.buy - c.sell
}

export function sortConsensus(rows: SectorConsensus[]): SectorConsensus[] {
  return [...rows].sort((a, b) => consensusNet(b) - consensusNet(a))
}

// ---------------------------------------------------------------------- //
// Market calendar
// ---------------------------------------------------------------------- //

export interface CalendarDay {
  /** `YYYY-MM-DD`, Eastern. */
  date: string
  events: CalendarEvent[]
}

/** Forward-looking only, grouped by session.
 *
 * PRD.md §8.3 says forward-looking, and this enforces it against the clock
 * rather than trusting the fixture to stay ahead of one — a hardcoded list
 * silently becomes a list of things that already happened, and a calendar
 * of the past is worse than no calendar because it still looks like a
 * warning.
 *
 * An all-day event (`at === null`) counts as upcoming for the whole of its
 * date: an ex-dividend date does not stop mattering at 9am. A timed event
 * drops off once its instant has passed.
 *
 * Within a day, timed events sort by time and all-day events lead — they
 * apply to the session as a whole, so filing them at midnight would both
 * imply a time they do not have and, in ET, put them on the wrong day. */
export function upcomingEvents(events: CalendarEvent[], now: string): CalendarDay[] {
  const today = etDate(now)
  const nowMs = Date.parse(now)

  const upcoming = events.filter((e) => (e.at === null ? e.date >= today : Date.parse(e.at) >= nowMs))

  const byDate = new Map<string, CalendarEvent[]>()
  for (const e of upcoming) {
    const day = byDate.get(e.date)
    if (day) day.push(e)
    else byDate.set(e.date, [e])
  }

  return [...byDate.entries()]
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, day]) => ({
      date,
      events: [...day].sort((a, b) => {
        if (a.at === null && b.at === null) return a.title.localeCompare(b.title)
        if (a.at === null) return -1
        if (b.at === null) return 1
        return a.at.localeCompare(b.at)
      }),
    }))
}

// ---------------------------------------------------------------------- //
// Label provenance and discovery (Phase 3 step 5)
// ---------------------------------------------------------------------- //

/** A labelling source, named. The tier (`SENTIMENT_TIER_LABEL`) says what
 * *kind* of source; this says which one. */
export const NEWS_SOURCE_LABEL: Record<NewsLabelSource, string> = {
  rules: 'Corollary rules',
  massive: 'Massive',
}

/** The rules tier's pattern families (`corollary/data/news/rules.py`'s
 * `RuleFamily`), in words. */
export const RULE_FAMILY_LABEL: Record<string, string> = {
  earnings: 'Earnings',
  guidance: 'Guidance',
  analyst_rating: 'Analyst rating',
  mna: 'M&A',
  secondary_offering: 'Secondary offering',
  buyback: 'Buyback',
  executive_departure: 'Executive departure',
  fda: 'FDA',
}

/** What flagged a mover, in words: the rule family for a rules reason, the
 * vendor for a Massive one. An unknown family — one the engine added after
 * this map — is spelled from its id rather than dropped, because a reason
 * with no name is a row nobody can evaluate. */
export function moverReasonName(reason: NewsMoverReason): string {
  if (reason.source === 'massive' || reason.family === null) {
    return NEWS_SOURCE_LABEL[reason.source]
  }
  const known = RULE_FAMILY_LABEL[reason.family]
  if (known !== undefined) return known
  const spelled = reason.family.replace(/_/g, ' ')
  return spelled.charAt(0).toUpperCase() + spelled.slice(1)
}

/** Which of the two empty states applies, or `null` when there are rows.
 *
 * A stale feed outranks an empty filter: with a firehose down, "nothing
 * passed" is not a finding — the names that would have passed may simply
 * not have arrived. */
export function moversEmptyState(movers: NewsMovers): 'stale' | 'none-passed' | null {
  if (movers.items.length > 0) return null
  return movers.feedsStale ? 'stale' : 'none-passed'
}

/** What a source's audit status says, as a sentence (Q22).
 *
 * **Keyed by every status, with `active` spelled out**, rather than an
 * if/else whose last branch is "passed". That fall-through used to tell the
 * reader any value it did not recognise — a status the engine added later, a
 * malformed payload — was an audit pass. */
export const SOURCE_STATUS_SENTENCE: Record<NewsSourceStatus, string> = {
  unaudited: 'No accuracy audit has run on this source yet.',
  active: 'This source passed its accuracy audit.',
  demoted: 'This source is demoted: its labels are shown for display only.',
}

/** The status sentence, or `null` for a null or unrecognised status — say
 * nothing rather than guess. A `Record` lookup alone is not enough: a value
 * outside the union arrives at runtime whatever the type says. */
export function sourceStatusSentence(status: string | null): string | null {
  if (status === null || !Object.prototype.hasOwnProperty.call(SOURCE_STATUS_SENTENCE, status)) {
    return null
  }
  return SOURCE_STATUS_SENTENCE[status as NewsSourceStatus]
}

/** The sources the served items report as `unaudited` — the displayed label
 * and the non-displayed one both — in `NEWS_SOURCE_LABEL`'s order.
 *
 * Read from what the server sent, never assumed: once step 6 audits a
 * source it drops out of this list, and the page's "unaudited" caption with
 * it. An item with no label reports nothing. */
export function unauditedSources(items: NewsItem[]): NewsLabelSource[] {
  const found = new Set<NewsLabelSource>()
  for (const item of items) {
    if (item.source !== null && item.sourceStatus === 'unaudited') found.add(item.source)
    const other = item.otherLabel
    if (other !== null && other.sourceStatus === 'unaudited') found.add(other.source)
  }
  return inSourceOrder(found)
}

/** The sources the Movers panel's **own** reasons report as `unaudited`, in
 * `NEWS_SOURCE_LABEL`'s order.
 *
 * Read from `/movers`, never borrowed from the feed. The feed's page is a
 * different set of articles: under the Unclassified filter, on a quiet watch
 * list, or after a feed error it carries no label at all, and the caption
 * would vanish over rows that all rest on unaudited labels; carrying only
 * rules labels beside a Massive-only mover, it would name rules alone and
 * imply Massive had passed an audit (Q22). */
export function unauditedMoverSources(movers: NewsMover[]): NewsLabelSource[] {
  const found = new Set<NewsLabelSource>()
  for (const mover of movers) {
    for (const reason of mover.reasons) {
      if (reason.sourceStatus === 'unaudited') found.add(reason.source)
    }
  }
  return inSourceOrder(found)
}

function inSourceOrder(found: Set<NewsLabelSource>): NewsLabelSource[] {
  return (Object.keys(NEWS_SOURCE_LABEL) as NewsLabelSource[]).filter((s) => found.has(s))
}

/** The visible "unaudited" caption, or `null` when no served label is
 * unaudited. Names the source when only one of them is, so a source a later
 * audit passed is never swept into the claim. */
export function unauditedCaption(sources: NewsLabelSource[]): string | null {
  if (sources.length === 0) return null
  if (sources.length === Object.keys(NEWS_SOURCE_LABEL).length) {
    return 'Labels are unaudited: no accuracy audit has run yet.'
  }
  return `${sources.map((s) => NEWS_SOURCE_LABEL[s]).join(' and ')} labels are unaudited: no accuracy audit has run on ${
    sources.length === 1 ? 'that source' : 'those sources'
  } yet.`
}

/** The three discovery firehoses, named. */
export const DISCOVERY_FEED_LABEL: Record<DiscoveryFeedName, string> = {
  alpaca_news: 'Alpaca news',
  finnhub_market: 'Finnhub market news',
  massive_news: 'Massive news',
}

/** "since Sep 25, 12:00 AM ET", or the stored history for `all`. */
export function sincePhrase(since: string | null): string {
  return since === null ? 'in the stored history' : `since ${formatDateTimeET(since)} ET`
}

/** The feeds-stale line. A stale feed that never delivered has no instant
 * to quote, so it is named instead of dated. */
export function staleLine(movers: NewsMovers): string {
  if (movers.staleSince !== null) {
    return `Discovery feeds stale since ${formatDateTimeET(movers.staleSince)} ET`
  }
  const never = movers.feeds.filter((f) => f.stale).map((f) => DISCOVERY_FEED_LABEL[f.feed])
  return `Discovery feeds stale — ${never.join(', ') || 'a feed'} has not delivered yet`
}

/** A mover's article time. Under `today` the clock alone is unambiguous;
 * under any longer lookback it is not — an article from nine days ago would
 * read "9:42 AM" — so the date rides with it, as `sincePhrase` does. Under
 * `all` the year rides too: labelled articles are retained indefinitely, so
 * "Sep 16" could be any September. The bounded lookbacks reach two weeks at
 * most, where the month and day cannot be ambiguous. */
export function moverArticleTime(time: string, lookback: NewsLookback): string {
  if (lookback === 'today') return formatTimeET(time)
  if (lookback === 'all') return formatSessionDateTimeET(time)
  return formatDateTimeET(time)
}

/** Why the tradeability check is not advancing, or `null` while it is.
 *
 * Said because the none-passed state otherwise implies the signalled names
 * are merely waiting their turn: with the job failing or never scheduled,
 * "awaiting a first check" waits forever. */
export function evaluationStuckLine(evaluation: DiscoveryEvaluation): string | null {
  if (!evaluation.scheduled) {
    return `The tradeability check (${evaluation.job}) is not scheduled, so signalled names are not being checked.`
  }
  if (evaluation.failing) {
    const last =
      evaluation.lastSuccess === null
        ? 'it has never succeeded'
        : `it last succeeded ${formatDateTimeET(evaluation.lastSuccess)} ET`
    return `The tradeability check (${evaluation.job}) is failing — ${last} — so signalled names are not being checked.`
  }
  return null
}
