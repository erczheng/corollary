import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { LiveStatus } from '../components/LiveStatus'
import { Pagination } from '../components/Pagination'
import { FixtureMarker } from '../components/FixtureMarker'
import { RequestFailed } from '../components/RequestFailed'
import { SentimentGauge } from '../components/SentimentGauge'
import { TableSkeleton } from '../components/Skeleton'
import { useUIStore } from '../lib/store'
import { useAddWatch, useNewsFeed, useRemoveWatch, useWatchList } from '../lib/queries'
import { CALENDAR_EVENTS, SECTOR_CONSENSUS, SOCIAL_ATTENTION, SOCIAL_AS_OF } from '../lib/mockData'
import {
  CALENDAR_TYPE_LABEL,
  SENTIMENT_CLASS,
  SENTIMENT_LABEL,
  SENTIMENT_SHORT,
  SENTIMENT_TIER_DETAIL,
  SENTIMENT_TIER_LABEL,
  type CalendarEvent,
  type CalendarEventType,
  type NewsItem,
  type NewsScope,
  type Sentiment,
} from '../lib/types'
import {
  DEFAULT_NEWS_FILTER,
  LOOKBACKS,
  LOOKBACK_LABEL,
  LOOKBACK_PHRASE,
  NEWS_SORT_LABEL,
  attentionVelocity,
  labeledShare,
  orderSectors,
  sortAttention,
  sortConsensus,
  upcomingEvents,
  type NewsFilter,
  type NewsSort,
} from '../lib/news'
import { formatCompactNumber, formatDateOnly, formatPct, formatTimeET, formatDateTimeET } from '../lib/format'

/** Deeper than the Markets tables, because the feed is now the tall column
 * of a two-column page rather than one panel among five stacked ones —
 * there is room, and paging a news feed every fifteen rows is more
 * interruption than navigation. */
const PAGE_SIZE = 25

/** The page is mixed since Phase 3 step 4: the feed and the watch list are
 * the engine's, the rest is still sample data. So the marker sits on each
 * fixture panel rather than on the title — the Settings precedent, where a
 * marker over the page would label real data as invented, which is worse
 * than no marker at all. */
const FIXTURE_DETAIL = {
  composite:
    'Sample data. The sentiment composite and its seven components are PRD §9 pipeline work that has not landed — nothing here was computed from a published headline.',
  social:
    'Sample data. Social attention is not wired to StockTwits yet — these velocities and message counts are invented.',
  consensus:
    'Sample data. Analyst consensus by sector is not wired to a provider yet — these ratings are invented.',
  calendar:
    'Sample data. The market calendar is not wired to a provider yet — these events are invented.',
} as const

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
  marker,
  children,
}: {
  id: string
  title: string
  meta: string
  /** A `FixtureMarker` while the panel is still sample data. Beside the
   * heading, never inside it, so the region's name stays the title. */
  marker?: ReactNode
  children: ReactNode
}) {
  return (
    <section
      aria-labelledby={`${id}-heading`}
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 id={`${id}-heading`} className="text-title-lg text-on-surface">
          {title}
        </h2>
        {marker}
      </div>
      <p className="mt-0.5 text-caption text-on-surface-variant">{meta}</p>
      {children}
    </section>
  )
}

// ---------------------------------------------------------------------- //
// Live feed — real since Phase 3 step 4
// ---------------------------------------------------------------------- //

/** Which tier labelled the item, or nothing while no tier has.
 *
 * Deliberately not a coloured mark. The sentiment beside it already owns
 * the colour in this row, and a second one would compete with the one
 * carrying the actual claim — this says *who said so*, which is supporting
 * evidence rather than the finding. **A null tier renders nothing**: in
 * step 4 no item is labelled, and a tag reading "undefined" (or a guessed
 * tier) would name a source that produced nothing. */
function TierTag({ item }: { item: NewsItem }) {
  if (item.tier === null) return null
  return (
    <span
      title={SENTIMENT_TIER_DETAIL[item.tier]}
      className="ml-2 text-caption text-on-surface-variant"
    >
      {SENTIMENT_TIER_LABEL[item.tier]}
      {item.demoted ? ' (demoted)' : null}
    </span>
  )
}

const SCOPE_LABEL: Record<NewsScope, string> = {
  watch: 'Watch list',
  all: 'Everything',
}

/** Watch list / everything. A control, so a 0.5rem rectangle rather than a
 * pill — pills on this page are status. */
function ScopeToggle({ scope, onChange }: { scope: NewsScope; onChange: (s: NewsScope) => void }) {
  return (
    <div
      role="group"
      aria-label="Which headlines the feed covers"
      className="flex rounded border border-outline bg-surface-container-low p-0.5 text-label-sm"
    >
      {(['watch', 'all'] as NewsScope[]).map((s) => (
        <button
          key={s}
          type="button"
          aria-pressed={scope === s}
          onClick={() => onChange(s)}
          className={
            scope === s
              ? 'rounded bg-primary px-2 py-0.5 text-on-primary'
              : 'rounded px-2 py-0.5 text-on-surface-variant hover:text-on-surface'
          }
        >
          {SCOPE_LABEL[s]}
        </button>
      ))}
    </div>
  )
}

/** The empty feed, worded for the question that was asked. An empty watch
 * list today and an empty store are different findings, and "no results"
 * says neither. */
function EmptyFeed({
  scope,
  filter,
  onWidenScope,
}: {
  scope: NewsScope
  filter: NewsFilter
  onWidenScope: () => void
}) {
  if (filter.sentiment !== null && filter.sentiment !== 'unclassified') {
    return (
      <p className="py-6 text-body-md text-on-surface-variant">
        Nothing is labelled {SENTIMENT_LABEL[filter.sentiment]} yet. Every headline reads{' '}
        <span className={SENTIMENT_CLASS.unclassified}>Unclassified</span> until sentiment
        labelling lands, so this filter matches nothing — it is not that the tape is quiet.
      </p>
    )
  }
  if (filter.sector !== null || filter.publisher !== null) {
    return (
      <p className="py-6 text-body-md text-on-surface-variant">
        No headlines match those filters {LOOKBACK_PHRASE[filter.lookback]}. They combine rather
        than replacing each other — widen the lookback or clear a filter first.
      </p>
    )
  }
  if (scope === 'watch') {
    return (
      <p className="py-6 text-body-md text-on-surface-variant">
        No headlines on your watch list {LOOKBACK_PHRASE[filter.lookback]}.{' '}
        <button
          type="button"
          onClick={onWidenScope}
          className="rounded text-primary underline underline-offset-2"
        >
          Switch to everything
        </button>{' '}
        to see stories about names you are not watching.
      </p>
    )
  }
  return (
    <p className="py-6 text-body-md text-on-surface-variant">
      {filter.lookback === 'all'
        ? 'No headlines have been stored yet. The feed fills as the news pipeline polls its sources.'
        : `No headlines stored ${LOOKBACK_PHRASE[filter.lookback]}, on any name. Widen the lookback.`}
    </p>
  )
}

function LiveFeed({ onUpdatedAt }: { onUpdatedAt: (at: string | null) => void }) {
  const [filter, setFilter] = useState<NewsFilter>(DEFAULT_NEWS_FILTER)
  const [scope, setScope] = useState<NewsScope>('watch')
  const [sort, setSort] = useState<NewsSort>('newest')
  const [page, setPage] = useState(1)

  // Every filter goes to the server. It is the one filter and the one sort:
  // nothing below narrows or re-orders `items`.
  const feed = useNewsFeed({
    scope,
    lookback: filter.lookback,
    sector: filter.sector,
    publisher: filter.publisher,
    sentiment: filter.sentiment,
    sort,
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
  })
  const watch = useWatchList()
  const data = feed.data

  // The freshness pill reports the last successful read of *this* feed.
  const updatedAt = feed.dataUpdatedAt > 0 ? new Date(feed.dataUpdatedAt).toISOString() : null
  useEffect(() => onUpdatedAt(updatedAt), [updatedAt, onUpdatedAt])

  // `GET /api/news` serves a page, not the set of values a filter could
  // take, so the dropdowns offer what this session has seen — and always
  // the current selection, so narrowing never empties its own dropdown.
  const seen = useRef({ sectors: new Set<string>(), publishers: new Set<string>() })
  for (const item of data?.items ?? []) {
    seen.current.sectors.add(item.sector)
    if (item.publisher !== null) seen.current.publishers.add(item.publisher)
  }
  if (filter.sector !== null) seen.current.sectors.add(filter.sector)
  if (filter.publisher !== null) seen.current.publishers.add(filter.publisher)
  const sectors = orderSectors(seen.current.sectors)
  const publishers = [...seen.current.publishers].sort((a, b) => a.localeCompare(b))

  // Written once so every control resets the page. Narrowing while deep in
  // the feed otherwise lands past the end of a shorter result set.
  const update = (patch: Partial<NewsFilter>) => {
    setFilter({ ...filter, ...patch })
    setPage(1)
  }
  const changeScope = (next: NewsScope) => {
    setScope(next)
    setPage(1)
  }

  const pageCount = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <section aria-labelledby="live-feed-heading">
      <SectionLabel
        id="live-feed-heading"
        aside={
          <div className="flex flex-wrap items-center gap-1.5">
            <ScopeToggle scope={scope} onChange={changeScope} />
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

      {data ? (
        <p className="mt-2 text-caption text-on-surface-variant">
          <span className="font-mono tabular-nums">{data.total.toLocaleString('en-US')}</span>{' '}
          {data.total === 1 ? 'headline' : 'headlines'}
          {data.scope === 'watch' ? ' on your watch list' : ' across every name'}
          {data.since !== null ? `, since ${formatDateTimeET(data.since)}` : ''}
        </p>
      ) : null}

      {data && !data.sectorsAvailable ? (
        <p className="mt-1 text-caption text-on-surface-variant">
          Sectors appear once the SPDR holdings seed is built — shown as Other for now, with
          market-wide stories under Macro.
        </p>
      ) : null}
      {scope === 'watch' && watch.data?.seedMissing ? (
        <p className="mt-1 text-caption text-on-surface-variant">
          The watch list does not yet include the sector leaders — they join once the SPDR
          holdings seed is built, so this scope is narrower than it will be.
        </p>
      ) : null}

      {feed.isError && data ? (
        // A failed refresh over a page already on screen: say so, keep the
        // page. Blanking it would read as "no news" rather than "no engine".
        <div className="mt-2">
          <RequestFailed error={feed.error} what="the news feed" />
        </div>
      ) : null}

      {feed.isPending || (feed.isPlaceholderData && feed.isFetching) ? (
        // A placeholder page belongs to the previous controls: worded from the
        // new scope and filter, its emptiness (or its rows) would be a claim
        // about a query that has not answered yet.
        <TableSkeleton rows={10} columns={6} label="Loading news feed" />
      ) : feed.isError && !data ? (
        <div className="py-6">
          <RequestFailed error={feed.error} what="the news feed" />
        </div>
      ) : !data || data.items.length === 0 ? (
        <EmptyFeed scope={scope} filter={filter} onWidenScope={() => changeScope('all')} />
      ) : (
        <>
          <table className="mt-1 w-full border-collapse">
            <thead>
              <tr>
                <th className={`${TH} text-left`}>Time</th>
                <th className={`${TH} text-left`}>Ticker</th>
                <th className={`${TH} w-full text-left`}>Headline</th>
                <th className={`${TH} text-left`}>Sector</th>
                <th className={`${TH} text-left`}>Sentiment</th>
                <th className={`${TH} text-right`}>Publisher</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
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
                  <td className={`${TD} max-w-0 text-body-sm text-on-surface`} title={item.headline}>
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="block truncate rounded hover:underline"
                    >
                      {item.headline}
                    </a>
                  </td>
                  <td className={`${TD} whitespace-nowrap text-caption text-on-surface-variant`}>
                    {item.sector}
                  </td>
                  {/* Small coloured text, not a pill — the convention
                      ExecutionsTable set for a status in a packed table.
                      Unclassified is `caution`, never `error`: nothing
                      having labelled a headline is the system working. */}
                  <td className={`${TD} whitespace-nowrap`}>
                    <span
                      title={SENTIMENT_LABEL[item.sentiment]}
                      className={`text-caption ${SENTIMENT_CLASS[item.sentiment]}`}
                    >
                      {SENTIMENT_SHORT[item.sentiment]}
                    </span>
                    <TierTag item={item} />
                  </td>
                  <td
                    className={`${TD} whitespace-nowrap text-right text-caption uppercase text-on-surface-variant`}
                  >
                    {item.publisher ?? (
                      <span title="The source named no publisher">
                        <span aria-hidden="true">—</span>
                        <span className="sr-only">No publisher named</span>
                      </span>
                    )}
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
// Manual watches — real since Phase 3 step 4
// ---------------------------------------------------------------------- //

/** The manual watches, removable, and a field to add one.
 *
 * **Remove does not confirm.** It is reversible — re-adding is one entry
 * away and removal keeps every stored headline — and it narrows what the
 * engine watches rather than widening anything. `settings.ts` confirms only
 * the risky direction, and nagging here is how the dialog that matters
 * gets dismissed by reflex. The server validates every add (rule 4); a
 * refusal renders its own sentence, in `error`, because it is a rule
 * outcome rather than a market fact. */
function ManualWatches() {
  const watch = useWatchList()
  const add = useAddWatch()
  const remove = useRemoveWatch()
  const [ticker, setTicker] = useState('')

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const value = ticker.trim()
    if (value === '') return
    add.mutate(value, { onSuccess: () => setTicker('') })
  }

  const list = watch.data

  return (
    <RailCard
      id="manual-watches"
      title="Watch list"
      meta={
        list
          ? `${list.countBeforePositions} of ${list.cap} symbols, before position underlyings`
          : 'Names you added by hand, on top of the Markets universe and sector leaders'
      }
    >
      {watch.isPending ? (
        <p className="mt-3 text-caption text-on-surface-variant">Loading the watch list…</p>
      ) : watch.isError ? (
        <div className="mt-3">
          <RequestFailed error={watch.error} what="the watch list" />
        </div>
      ) : list ? (
        <>
          {list.seedMissing ? (
            <p className="mt-2 text-caption text-on-surface-variant">
              The SPDR holdings seed is not built, so sector leaders are not counted yet — this
              count will rise when it is.
            </p>
          ) : null}
          {list.manual.length === 0 ? (
            <p className="mt-3 text-caption text-on-surface-variant">
              No manual watches. The watch list already covers the Markets universe and your open
              positions; add a name here to follow one outside them.
            </p>
          ) : (
            <ul aria-label="Manual watches" className="mt-3 divide-y divide-outline-variant">
              {list.manual.map((m) => (
                <li key={m.ticker} className="flex items-center gap-2 py-1">
                  <span className="text-label-md text-on-surface">{m.ticker}</span>
                  <span className="ml-auto text-caption text-on-surface-variant">
                    added {formatDateTimeET(m.addedAt)}
                  </span>
                  <button
                    type="button"
                    onClick={() => remove.mutate(m.ticker)}
                    disabled={remove.isPending && remove.variables === m.ticker}
                    aria-label={`Remove ${m.ticker} from the watch list`}
                    className="rounded px-2 py-0.5 text-label-sm text-on-surface-variant hover:bg-surface-container hover:text-on-surface disabled:opacity-50"
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          {remove.isError ? (
            <div className="mt-2">
              <RequestFailed error={remove.error} what="the watch list" />
            </div>
          ) : null}
          <form onSubmit={submit} className="mt-3 flex gap-2">
            <input
              value={ticker}
              onChange={(e) => {
                setTicker(e.target.value.toUpperCase())
                if (add.isError) add.reset()
              }}
              aria-label="Ticker to watch"
              placeholder="Add ticker"
              maxLength={32}
              className="min-w-0 flex-1 rounded border border-outline bg-surface px-2 py-1 font-mono text-caption text-on-surface placeholder:text-on-surface-variant focus:border-primary"
            />
            <button
              type="submit"
              disabled={add.isPending || ticker.trim() === ''}
              className="rounded bg-primary px-3 py-1 text-label-sm text-on-primary disabled:opacity-50"
            >
              Watch
            </button>
          </form>
          {add.isError ? (
            <div className="mt-2">
              <RequestFailed error={add.error} what="the watch list" />
            </div>
          ) : !list.assetListAvailable ? (
            <p className="mt-2 text-caption text-on-surface-variant">
              The asset list has not been fetched yet, so an add cannot be validated and is
              refused until the daily refresh has run.
            </p>
          ) : null}
        </>
      ) : null}
    </RailCard>
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
      marker={<FixtureMarker detail={FIXTURE_DETAIL.social} />}
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
      marker={<FixtureMarker detail={FIXTURE_DETAIL.consensus} />}
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
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-caption text-on-surface-variant">
              Scheduled ahead, forward-looking only
            </span>
            <FixtureMarker detail={FIXTURE_DETAIL.calendar} />
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
  // The feed is the only live thing on this page, so the freshness pill
  // reports the feed's last successful read and nothing else. The rail's
  // panels refresh daily or monthly, or are still fixtures.
  const [feedAt, setFeedAt] = useState<string | null>(null)
  const onFeedUpdated = useCallback((at: string | null) => setFeedAt(at), [])

  // The calendar is still a fixture and runs off the fixture's clock:
  // MARKET_TODAY is the fixture's today, and measured against the wall
  // clock "forward-looking only" would quietly empty the panel.
  const fixtureFeed = useUIStore((s) => s.newsFeed)
  const now = fixtureFeed[0]?.time ?? new Date().toISOString()

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          {/* No marker on the title: the page is mixed, so each fixture
              panel carries its own (see FIXTURE_DETAIL). */}
          <h1 className="text-display-lg text-on-surface">News</h1>
          <p className="mt-1 text-body-md text-on-surface-variant">
            Market intelligence, sentiment and what is scheduled ahead.
          </p>
        </div>
        <LiveStatus at={feedAt} kind="poll" />
      </div>

      {/* The rail comes first in the source, so a screen reader and a narrow
          viewport both meet the market's summary before its detail. It sits
          left from `lg` up and stacks above below that. */}
      <div className="mt-8 grid items-start gap-6 lg:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]">
        <aside className="grid gap-4">
          <SentimentGauge marker={<FixtureMarker detail={FIXTURE_DETAIL.composite} />} />
          <ManualWatches />
          <SocialAttention />
          <TopRatedBySector />
        </aside>

        <div>
          <LiveFeed onUpdatedAt={onFeedUpdated} />
          <MarketCalendar now={now} />
        </div>
      </div>
    </div>
  )
}
