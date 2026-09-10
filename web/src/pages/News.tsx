import { useState, type ReactNode } from 'react'
import { LiveStatus } from '../components/LiveStatus'
import { Pagination } from '../components/Pagination'
import { SentimentGauge } from '../components/SentimentGauge'
import { TableSkeleton } from '../components/Skeleton'
import { usePagination } from '../hooks/usePagination'
import { useNewsPoll } from '../hooks/useNewsPoll'
import { useUIStore } from '../lib/store'
import {
  CALENDAR_EVENTS,
  CALENDAR_TYPE_LABEL,
  SECTOR_CONSENSUS,
  SENTIMENT_CLASS,
  SENTIMENT_LABEL,
  SENTIMENT_SHORT,
  SENTIMENT_TIER_DETAIL,
  SENTIMENT_TIER_LABEL,
  SOCIAL_ATTENTION,
  SOCIAL_AS_OF,
  type CalendarEvent,
  type CalendarEventType,
  type NewsItem,
  type Sentiment,
} from '../lib/mockData'
import {
  DEFAULT_NEWS_FILTER,
  LOOKBACKS,
  LOOKBACK_LABEL,
  NEWS_SORT_LABEL,
  attentionVelocity,
  etDate,
  filterNews,
  labeledShare,
  newsPublishers,
  newsSectors,
  sortAttention,
  sortConsensus,
  sortNews,
  upcomingEvents,
  type NewsFilter,
  type NewsSort,
} from '../lib/news'
import {
  formatCompactNumber,
  formatDateOnly,
  formatPct,
  formatTimeET,
  formatDateTimeET,
} from '../lib/format'

/** Deeper than the Markets tables, because the feed is now the tall column
 * of a two-column page rather than one panel among five stacked ones —
 * there is room, and paging a news feed every fifteen rows is more
 * interruption than navigation. */
const PAGE_SIZE = 25

/** How often the terminal asks for news.
 *
 * Fifteen seconds, and deliberately slower than either price feed — those
 * exist because a quote is stale the moment after it arrives, while a
 * headline published at 10:04 is the same headline at 10:05. Polling this
 * at 400ms would be asking a question whose answer changes a few times an
 * hour. */
const POLL_MS = 15_000

/** Dense table chrome, one step tighter than Activity's ledger.
 *
 * `py-1.5` and small type rather than `py-2` and `text-body-md`: this page
 * is a scanning surface — the question is "what happened", answered by
 * reading down a column of headlines — where Activity's is a reconciliation
 * surface you read one row of at a time. ExecutionsTable already set the
 * precedent for small text in a packed table. */
const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'

const SELECT =
  'rounded border border-outline bg-surface px-2 py-1 text-caption text-on-surface focus:border-primary'

const SENTIMENTS: Sentiment[] = ['bullish', 'bearish', 'neutral', 'unclassified']

/** A section label in the main column — small, uppercase, no card around
 * it. The feed and the calendar are the page's subject rather than panels
 * on it, so boxing them would add a border for nothing; the rail's cards
 * are what they sit beside. */
function SectionLabel({
  id,
  children,
  aside,
}: {
  id: string
  children: ReactNode
  aside?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm pb-2">
      <h2 id={id} className="text-label-md uppercase tracking-wide text-on-surface-variant">
        {children}
      </h2>
      {aside}
    </div>
  )
}

/** A card in the left rail. */
function RailCard({
  id,
  title,
  meta,
  children,
}: {
  id: string
  title: string
  meta: string
  children: ReactNode
}) {
  return (
    <section
      aria-labelledby={`${id}-heading`}
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <h2 id={`${id}-heading`} className="text-title-lg text-on-surface">
        {title}
      </h2>
      <p className="mt-0.5 text-caption text-on-surface-variant">{meta}</p>
      {children}
    </section>
  )
}

// ---------------------------------------------------------------------- //
// Live feed
// ---------------------------------------------------------------------- //

/** Which tier labelled the item.
 *
 * Deliberately not a coloured mark. The sentiment beside it already owns
 * the colour in this row, and a second one would compete with the one
 * carrying the actual claim — this says *who said so*, which is supporting
 * evidence rather than the finding. */
function TierTag({ tier }: { tier: NewsItem['tier'] }) {
  return (
    <span title={SENTIMENT_TIER_DETAIL[tier]} className="text-caption text-on-surface-variant">
      {SENTIMENT_TIER_LABEL[tier]}
    </span>
  )
}

function LiveFeed({ loading }: { loading: boolean }) {
  const feed = useUIStore((s) => s.newsFeed)
  const [filter, setFilter] = useState<NewsFilter>(DEFAULT_NEWS_FILTER)
  const [sort, setSort] = useState<NewsSort>('newest')

  // The feed's own clock, not the wall clock. MARKET_TODAY is the fixture's
  // today and the machine's is not, so a lookback measured against
  // `Date.now()` would return nothing at every step but "All time".
  const now = feed[0]?.time ?? new Date().toISOString()

  const sectors = newsSectors(feed)
  const publishers = newsPublishers(feed)
  const rows = sortNews(filterNews(feed, filter, now), sort)
  const { page, pageCount, pageItems, setPage } = usePagination(rows, PAGE_SIZE)

  // Written once so every control resets the page. Narrowing while deep in
  // the feed otherwise lands on the last page of a shorter result set,
  // which reads as "no headlines".
  const update = (patch: Partial<NewsFilter>) => {
    setFilter({ ...filter, ...patch })
    setPage(1)
  }

  const filtered =
    filter.sector !== null ||
    filter.publisher !== null ||
    filter.sentiment !== null ||
    filter.lookback !== 'all'

  return (
    <section aria-labelledby="live-feed-heading">
      <SectionLabel
        id="live-feed-heading"
        aside={
          <div className="flex flex-wrap items-center gap-1.5">
            <select
              value={filter.sector ?? 'all'}
              onChange={(e) => update({ sector: e.target.value === 'all' ? null : e.target.value })}
              aria-label="Filter headlines by sector"
              className={SELECT}
            >
              <option value="all">All sectors</option>
              {sectors.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <select
              value={filter.publisher ?? 'all'}
              onChange={(e) =>
                update({ publisher: e.target.value === 'all' ? null : e.target.value })
              }
              aria-label="Filter headlines by publisher"
              className={SELECT}
            >
              <option value="all">All publishers</option>
              {publishers.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <select
              value={filter.sentiment ?? 'all'}
              onChange={(e) =>
                update({
                  sentiment: e.target.value === 'all' ? null : (e.target.value as Sentiment),
                })
              }
              aria-label="Filter headlines by sentiment"
              className={SELECT}
            >
              <option value="all">All sentiment</option>
              {SENTIMENTS.map((s) => (
                <option key={s} value={s}>
                  {SENTIMENT_LABEL[s]}
                </option>
              ))}
            </select>
            <select
              value={filter.lookback}
              onChange={(e) => update({ lookback: e.target.value as NewsFilter['lookback'] })}
              aria-label="How far back the feed reaches"
              className={SELECT}
            >
              {LOOKBACKS.map((l) => (
                <option key={l} value={l}>
                  {LOOKBACK_LABEL[l]}
                </option>
              ))}
            </select>
            <select
              value={sort}
              onChange={(e) => {
                setSort(e.target.value as NewsSort)
                setPage(1)
              }}
              aria-label="Sort headlines by time"
              className={SELECT}
            >
              {(Object.keys(NEWS_SORT_LABEL) as NewsSort[]).map((s) => (
                <option key={s} value={s}>
                  {NEWS_SORT_LABEL[s]}
                </option>
              ))}
            </select>
          </div>
        }
      >
        Latest intel
      </SectionLabel>

      <p className="mt-2 text-caption text-on-surface-variant">
        {filtered
          ? `${rows.length} of ${feed.length} headlines`
          : // The span the corpus actually covers, read off the corpus —
            // not a hardcoded "last 2 weeks", which would slowly stop being
            // true as the feed grows. Read off `feed`, which is always
            // newest-first, rather than `rows`, whose ends swap over when
            // the sort is flipped to oldest-first.
            `${feed.length} headlines, ${formatDateOnly(etDate(feed[feed.length - 1].time))} to today`}
      </p>

      {loading ? (
        <TableSkeleton rows={10} columns={5} label="Loading news feed" />
      ) : rows.length === 0 ? (
        <p className="py-6 text-body-md text-on-surface-variant">
          No headlines match those filters. They combine rather than replacing each other, so
          narrowing sector <em>and</em> sentiment <em>and</em> a short lookback can empty the feed
          even when each alone would not — widen the lookback first.
        </p>
      ) : (
        <>
          <table className="mt-1 w-full border-collapse">
            <thead>
              <tr>
                <th className={`${TH} text-left`}>Time</th>
                <th className={`${TH} text-left`}>Ticker</th>
                <th className={`${TH} w-full text-left`}>Headline</th>
                <th className={`${TH} text-left`}>Sentiment</th>
                <th className={`${TH} text-right`}>Publisher</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((item) => (
                <tr
                  key={item.id}
                  className="h-8 border-t border-outline/10 hover:bg-surface-container-low"
                >
                  <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                    {formatDateTimeET(item.time)}
                  </td>
                  <td className={`${TD} whitespace-nowrap text-label-md text-on-surface`}>
                    {/* MARKET is not a ticker and should not read as one —
                        it is the bucket for a story about no single name. */}
                    {item.ticker === 'MARKET' ? (
                      <span className="text-on-surface-variant">MARKET</span>
                    ) : (
                      item.ticker
                    )}
                  </td>
                  {/* The sector rides along as the row's title rather than a
                      second line. One line per headline is what lets a
                      screenful be scanned, and the sector is a filter — the
                      dropdown answers "show me Technology" better than a
                      label repeated under every row ever could. */}
                  <td
                    className={`${TD} max-w-0 text-body-sm text-on-surface`}
                    title={`${item.headline} — ${item.sector}`}
                  >
                    <span className="block truncate">{item.headline}</span>
                  </td>
                  {/* Small coloured text, not a pill — the convention
                      ExecutionsTable set for a status in a packed table. A
                      chip per row sets the row height for a one-word label.
                      The four-letter form keeps the column narrow, and the
                      full word is in the title. */}
                  <td className={`${TD} whitespace-nowrap`}>
                    <span
                      title={SENTIMENT_LABEL[item.sentiment]}
                      className={`text-caption ${SENTIMENT_CLASS[item.sentiment]}`}
                    >
                      {SENTIMENT_SHORT[item.sentiment]}
                    </span>
                    <span className="ml-2">
                      <TierTag tier={item.tier} />
                    </span>
                  </td>
                  <td
                    className={`${TD} whitespace-nowrap text-right text-caption uppercase text-on-surface-variant`}
                  >
                    {item.publisher}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination page={page} pageCount={pageCount} onChange={setPage} />
        </>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------- //
// Social attention
// ---------------------------------------------------------------------- //

const RAIL_TH = 'pb-1 text-caption uppercase text-on-surface-variant'

function SocialAttention() {
  const rows = sortAttention(SOCIAL_ATTENTION)

  return (
    <RailCard
      id="social-attention"
      title="Social Attention"
      meta={`StockTwits mention velocity, as of ${formatTimeET(SOCIAL_AS_OF)}`}
    >
      <table className="mt-3 w-full border-collapse">
        <thead>
          <tr>
            <th className={`${RAIL_TH} text-left`}>Ticker</th>
            <th className={`${RAIL_TH} text-right`} title="Mentions against the 30-day baseline">
              Vel.
            </th>
            {/* Sample size and labelled count are both required by PRD.md
                §8.3, and neither substitutes for the other: the sample says
                how much conversation there was, the labelled count says how
                much of it the sentiment was actually computed from. A share
                alone hides the first — 40% of 80 messages and 40% of 4,000
                print identically. */}
            <th className={`${RAIL_TH} text-right`} title="Messages in the last session">
              Msgs
            </th>
            <th className={`${RAIL_TH} text-right`} title="Messages carrying a user label">
              Lbl.
            </th>
            <th className={`${RAIL_TH} text-right`} title="Sentiment">
              Sent.
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => {
            const velocity = attentionVelocity(item)
            return (
              <tr key={item.ticker} className="border-t border-outline/10">
                <td className="py-1 text-label-md text-on-surface">{item.ticker}</td>
                {/* Unusual attention carries no direction — a name at 4x its
                    baseline is as likely to be collapsing as running, so
                    this is never bullish or bearish. `on-accent-container`
                    rather than bare `accent`, which is 2.53:1 on surface. */}
                <td
                  className={`py-1 text-right text-data-md ${
                    velocity >= 2 ? 'font-semibold text-on-accent-container' : 'text-on-surface'
                  }`}
                  title={`${formatCompactNumber(item.mentions)} against a ${formatCompactNumber(
                    item.baselineMentions,
                  )} baseline`}
                >
                  {velocity.toFixed(2)}×
                </td>
                <td className="py-1 text-right text-data-md text-on-surface-variant">
                  {formatCompactNumber(item.sampleSize)}
                </td>
                {/* The subset the sentiment is actually computed over. Shown
                    because a direction drawn from 11% of messages and one
                    drawn from 40% are not the same claim, and the panel has
                    no business rendering them identically. The share is in
                    the title rather than the cell — the count is what
                    PRD.md §8.3 asks for, and two numbers plus a percentage
                    is more arithmetic than a rail column can carry. */}
                <td
                  className="py-1 text-right text-data-md text-on-surface-variant"
                  title={`${item.labeledCount.toLocaleString('en-US')} of ${item.sampleSize.toLocaleString(
                    'en-US',
                  )} messages carried a user label — ${formatPct(labeledShare(item) * 100)}`}
                >
                  {formatCompactNumber(item.labeledCount)}
                </td>
                <td className="py-1 text-right">
                  <span
                    title={SENTIMENT_LABEL[item.sentiment]}
                    className={`text-caption ${SENTIMENT_CLASS[item.sentiment]}`}
                  >
                    {SENTIMENT_SHORT[item.sentiment]}
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {/* PRD.md §8.3 is explicit that this is context and never a trigger,
          and the place to say so is here rather than in a doc nobody has
          open while looking at the panel. */}
      <p className="mt-2 border-t border-outline-warm pt-2 text-caption text-on-surface-variant">
        Mentions against each name&rsquo;s own 30-day baseline, not the field.{' '}
        <strong className="font-semibold text-on-surface">Context, never a trade trigger.</strong>
      </p>
    </RailCard>
  )
}

// ---------------------------------------------------------------------- //
// Top rated by sector
// ---------------------------------------------------------------------- //

function TopRatedBySector() {
  const rows = sortConsensus(SECTOR_CONSENSUS)

  return (
    <RailCard
      id="sector-consensus"
      title="Top rated by sector"
      meta={`Analyst consensus, monthly — as of ${formatDateOnly(rows[0].asOf)}`}
    >
      <table className="mt-3 w-full border-collapse">
        <thead>
          <tr>
            <th className={`${RAIL_TH} text-left`}>Sector</th>
            <th className={`${RAIL_TH} text-left`}>Lead</th>
            <th className={`${RAIL_TH} w-full text-left`}>Buy / Hold / Sell</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.sector} className="border-t border-outline/10">
              <td
                className="py-1 pr-2 text-caption text-on-surface"
                title={`${c.sector} · ${c.etf}`}
              >
                {c.sector}
              </td>
              <td className="py-1 pr-2 text-caption text-on-surface-variant">{c.leader}</td>
              <td className="py-1">
                {/* A stacked bar *and* the figures, not one or the other.
                    The proportion is read faster as a length than as
                    arithmetic, but colour and length alone are exactly what
                    DESIGN.md forbids as the only signal — in grayscale, or
                    to a red-green colourblind reader, a bar of three
                    unlabelled segments says nothing about which end is
                    which. The numbers ride underneath in the same order the
                    header names them. */}
                <div
                  className="flex h-1.5 overflow-hidden rounded-full"
                  role="img"
                  aria-label={`${c.sector}: ${c.buy}% buy, ${c.hold}% hold, ${c.sell}% sell`}
                >
                  <span className="bg-bullish" style={{ width: `${c.buy}%` }} />
                  <span className="bg-neutral" style={{ width: `${c.hold}%` }} />
                  <span className="bg-bearish" style={{ width: `${c.sell}%` }} />
                </div>
                <p className="mt-0.5 text-data-md text-on-surface-variant">
                  <span className="text-bullish">{c.buy}</span>
                  {' / '}
                  {c.hold}
                  {' / '}
                  <span className="text-bearish">{c.sell}</span>
                </p>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </RailCard>
  )
}

// ---------------------------------------------------------------------- //
// Market calendar
// ---------------------------------------------------------------------- //

/** The order event types read best in within a day. */
const TYPE_ORDER: CalendarEventType[] = [
  'economic',
  'central-bank',
  'earnings',
  'dividend',
  'geopolitical',
]

/** Groups a day's events by type.
 *
 * The type is printed once per group rather than as a mark on every row,
 * which is what lets a day fit in a card — three earnings calls under one
 * EARNINGS heading, not three rows each carrying the same word. */
function groupByType(events: CalendarEvent[]): [CalendarEventType, CalendarEvent[]][] {
  return TYPE_ORDER.map(
    (type) => [type, events.filter((e) => e.type === type)] as [CalendarEventType, CalendarEvent[]],
  ).filter(([, group]) => group.length > 0)
}

const DAY_HEADER = new Intl.DateTimeFormat('en-US', {
  timeZone: 'UTC',
  weekday: 'short',
  month: 'short',
  day: 'numeric',
})

/** A day's heading. Formatted in **UTC** because `date` is a bare
 * `YYYY-MM-DD` — rendered in ET it would show the day before, the same trap
 * `formatExpiry` documents. */
function dayHeading(date: string): string {
  return DAY_HEADER.format(new Date(`${date}T00:00:00Z`)).toUpperCase()
}

function MarketCalendar({ now }: { now: string }) {
  const days = upcomingEvents(CALENDAR_EVENTS, now)

  return (
    <section aria-labelledby="market-calendar-heading" className="mt-8">
      <SectionLabel
        id="market-calendar-heading"
        aside={
          <span className="text-caption text-on-surface-variant">
            Scheduled ahead, forward-looking only
          </span>
        }
      >
        Market calendar
      </SectionLabel>

      {days.length === 0 ? (
        <p className="py-6 text-body-md text-on-surface-variant">
          Nothing scheduled ahead. This calendar is forward-looking only, so an empty panel means
          the next event is beyond the loaded window rather than that the week is quiet.
        </p>
      ) : (
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {days.map((day) => (
            <div
              key={day.date}
              className="rounded-lg border border-outline-warm bg-surface-container-lowest p-3"
            >
              <p className="text-caption uppercase tracking-wide text-on-surface">
                {dayHeading(day.date)}
              </p>
              <div className="mt-2 space-y-2">
                {groupByType(day.events).map(([type, group]) => (
                  <div key={type}>
                    <p className="text-caption uppercase tracking-wide text-on-surface-variant">
                      {CALENDAR_TYPE_LABEL[type]}
                    </p>
                    {group.map((e) => (
                      <p key={e.id} className="text-caption text-on-surface">
                        {/* An all-day event never had a time. Printing a
                            placeholder midnight would both invent one and,
                            in ET, put it on the previous evening. */}
                        <span className="text-on-surface-variant">
                          {e.at === null ? 'All day' : formatTimeET(e.at)}
                        </span>{' '}
                        {e.ticker ? <span className="font-semibold">{e.ticker}</span> : null}{' '}
                        {e.title}
                      </p>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------------- //

export function News() {
  const lastNewsAt = useUIStore((s) => s.lastNewsAt)
  const feed = useUIStore((s) => s.newsFeed)

  useNewsPoll(POLL_MS)

  // Loading is a real condition, not a timer — until the first poll returns
  // there is nothing current to show. Same rule Activity and Markets
  // follow.
  const loading = lastNewsAt === null

  // The calendar runs off the feed's clock for the same reason the lookback
  // does: MARKET_TODAY is the fixture's today. Measured against the wall
  // clock, "forward-looking only" would quietly empty the panel the moment
  // the real date passed the fixture's.
  const now = feed[0]?.time ?? new Date().toISOString()

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-display-lg text-on-surface">News</h1>
          <p className="mt-1 text-body-md text-on-surface-variant">
            Market intelligence, sentiment and what is scheduled ahead.
          </p>
        </div>
        {/* Scoped to the feed, and the tooltip says so. The rail's panels
            refresh daily or monthly, so one pill covering the page would be
            claiming a freshness three of them do not have. */}
        <LiveStatus at={lastNewsAt} kind="poll" />
      </div>

      {/* The rail comes first in the source, so a screen reader and a narrow
          viewport both meet the market's summary before its detail. It sits
          left from `lg` up and stacks above below that. */}
      <div className="mt-8 grid items-start gap-6 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]">
        <aside className="grid gap-4">
          <SentimentGauge />
          <SocialAttention />
          <TopRatedBySector />
        </aside>

        <div>
          <LiveFeed loading={loading} />
          <MarketCalendar now={now} />
        </div>
      </div>
    </div>
  )
}
