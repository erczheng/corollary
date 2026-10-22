import {
  Fragment,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from 'react'
import { LiveStatus } from '../components/LiveStatus'
import { Pagination } from '../components/Pagination'
import { FixtureMarker } from '../components/FixtureMarker'
import { MarketCalendar } from '../components/MarketCalendar'
import { SectionLabel } from '../components/SectionLabel'
import { RequestFailed } from '../components/RequestFailed'
import { SentimentGauge } from '../components/SentimentGauge'
import { TableSkeleton } from '../components/Skeleton'
import {
  useAddWatch,
  useNewsFeed,
  useNewsMovers,
  useRemoveWatch,
  useWatchList,
} from '../lib/queries'
import { SECTOR_CONSENSUS, SOCIAL_ATTENTION, SOCIAL_AS_OF } from '../lib/mockData'
import {
  SENTIMENT_CLASS,
  SENTIMENT_LABEL,
  SENTIMENT_SHORT,
  SENTIMENT_TIER_DETAIL,
  SENTIMENT_TIER_LABEL,
  type MoverDirection,
  type NewsItem,
  type NewsLabelSource,
  type NewsLookback,
  type NewsMover,
  type NewsMoverReason,
  type NewsMovers,
  type NewsScope,
  type Sentiment,
} from '../lib/types'
import {
  DEFAULT_NEWS_FILTER,
  LOOKBACKS,
  LOOKBACK_LABEL,
  LOOKBACK_PHRASE,
  NEWS_SORT_LABEL,
  NEWS_SOURCE_LABEL,
  attentionVelocity,
  labeledShare,
  evaluationStuckLine,
  moverArticleTime,
  moverReasonName,
  moversEmptyState,
  orderSectors,
  sincePhrase,
  sourceStatusSentence,
  staleLine,
  sortAttention,
  sortConsensus,
  unauditedCaption,
  unauditedMoverSources,
  unauditedSources,
  type NewsFilter,
  type NewsSort,
} from '../lib/news'
import {
  formatCompactNumber,
  formatDateOnly,
  formatDateTimeET,
  formatInteger,
  formatPct,
  formatSessionDay,
  formatTimeET,
  formatUsdDecimalString,
} from '../lib/format'

/** Deeper than the Markets tables, because the feed is now the tall column
 * of a two-column page rather than one panel among five stacked ones —
 * there is room, and paging a news feed every fifteen rows is more
 * interruption than navigation. */
const PAGE_SIZE = 25

/** The page is mixed since Phase 3 step 4: the feed and the watch list are
 * the engine's, and since step 7 so is the market calendar
 * (`components/MarketCalendar.tsx`, unmarked); the composite, social and
 * consensus panels are still sample data. So the marker sits on each
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

/** Which tier labelled the item, or an em dash while no source has.
 *
 * Deliberately not a coloured mark. The sentiment beside it already owns
 * the colour in this row, and a second one would compete with the one
 * carrying the actual claim — this says *who said so*, which is supporting
 * evidence rather than the finding. **A null tier renders an em dash**,
 * the publisher cell's convention: the tier is null exactly when the item
 * is unclassified (decision 17), and a tag reading "undefined" — or a
 * guessed tier — would name a source that produced nothing. */
function TierTag({ item }: { item: NewsItem }) {
  if (item.tier === null || item.source === null) {
    return (
      <span title="No source labelled this item" className="ml-2 text-caption text-on-surface-variant">
        <span aria-hidden="true">—</span>
        <span className="sr-only">No tier</span>
      </span>
    )
  }
  // The tier says what kind of source; the source says which one. For the
  // rules tier they are the same thing, so the source is named once, in the
  // title — "Rules · Corollary rules" in a packed cell would be noise.
  const tier = SENTIMENT_TIER_LABEL[item.tier]
  const source = NEWS_SOURCE_LABEL[item.source]
  // A null or unrecognised status says nothing — never a guessed "passed".
  const status = sourceStatusSentence(item.sourceStatus)
  return (
    <span
      title={`${SENTIMENT_TIER_DETAIL[item.tier]} Source: ${source}.${status ? ` ${status}` : ''}`}
      className="ml-2 text-caption text-on-surface-variant"
    >
      {tier}
      {item.source === 'massive' ? ` · ${source}` : <span className="sr-only"> · {source}</span>}
      {/* Q22: keyed on `demoted` alone. `unaudited` is not "not demoted"
          and is not "demoted" either — it says nothing, because nothing
          has been measured yet. */}
      {item.sourceStatus === 'demoted' ? ' (demoted · display only)' : null}
    </span>
  )
}

/** The label that lost display, when both sources labelled the item.
 *
 * A button rather than a bare tooltip, so the disagreement is reachable
 * from the keyboard and on touch — `title` alone is mouse-only. It expands
 * a line under the row rather than replacing the sentiment: decision 4 picks
 * the rules label, and this is evidence about that pick, not a second
 * verdict competing with it. */
function OtherLabelToggle({
  item,
  open,
  onToggle,
}: {
  item: NewsItem
  open: boolean
  onToggle: () => void
}) {
  const other = item.otherLabel
  if (other === null) return null
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      aria-controls={`other-label-${item.id}`}
      title={`${NEWS_SOURCE_LABEL[other.source]} labelled this ${SENTIMENT_LABEL[other.sentiment]}${
        other.reasoning ? `: ${other.reasoning}` : ''
      }`}
      className="ml-2 rounded text-caption text-on-surface-variant underline decoration-dotted underline-offset-2 hover:text-on-surface"
    >
      also{' '}
      <span className={SENTIMENT_CLASS[other.sentiment]}>{SENTIMENT_SHORT[other.sentiment]}</span>
      <span className="sr-only">
        {' '}
        — {NEWS_SOURCE_LABEL[other.source]}&rsquo;s label, not displayed
      </span>
    </button>
  )
}

function OtherLabelDetail({ item }: { item: NewsItem }) {
  const other = item.otherLabel
  if (other === null) return null
  return (
    <p id={`other-label-${item.id}`} className="text-caption text-on-surface-variant">
      {NEWS_SOURCE_LABEL[other.source]} ({SENTIMENT_TIER_LABEL[other.tier]}) labelled this{' '}
      <span className={SENTIMENT_CLASS[other.sentiment]}>{SENTIMENT_LABEL[other.sentiment]}</span>
      {other.reasoning ? <> — &ldquo;{other.reasoning}&rdquo;</> : null}. Not displayed: the{' '}
      {item.tier === null ? 'other' : SENTIMENT_TIER_LABEL[item.tier].toLowerCase()} label wins
      when both sources speak.
    </p>
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
        No headlines labelled {SENTIMENT_LABEL[filter.sentiment]}{' '}
        {LOOKBACK_PHRASE[filter.lookback]}
        {scope === 'watch' ? ' on your watch list' : ''}. Most stories match no rules pattern
        and carry no vendor insight, so{' '}
        <span className={SENTIMENT_CLASS.unclassified}>Unclassified</span> is the common case —
        widen the lookback or clear the filter.
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

/** Sectors come from the SPDR holdings seed; without it every name files as
 * Other. One sentence for the feed and the movers panel, which both read the
 * same seed. */
const SECTORS_PENDING_CAPTION =
  'Sectors appear once the SPDR holdings seed is built — shown as Other for now, with market-wide stories under Macro.'

/** The visible "unaudited" line — the `title` on a tier tag reaches neither
 * keyboard, touch nor screen-reader users. Rendered only while a served
 * label reports `unaudited`, so it leaves on its own once step 6 audits. */
function UnauditedCaption({ sources }: { sources: NewsLabelSource[] }) {
  const caption = unauditedCaption(sources)
  if (caption === null) return null
  return <p className="mt-1 text-caption text-on-surface-variant">{caption}</p>
}

function LiveFeed({
  lookback,
  onLookbackChange,
  onUpdatedAt,
}: {
  /** Owned by the page: the movers panel follows the same control (spec,
   * decision 21), so the two cannot answer for different windows. */
  lookback: NewsLookback
  onLookbackChange: (l: NewsLookback) => void
  onUpdatedAt: (at: string | null) => void
}) {
  const [narrowing, setNarrowing] = useState<NewsFilter>(DEFAULT_NEWS_FILTER)
  const filter: NewsFilter = { ...narrowing, lookback }
  const [scope, setScope] = useState<NewsScope>('watch')
  const [sort, setSort] = useState<NewsSort>('newest')
  const [page, setPage] = useState(1)
  // Which rows have their non-displayed label expanded. Keyed by id, so a
  // poll that inserts rows above does not move the expansion to another one.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())
  const toggleExpanded = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

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

  // This feed's own caption only. The movers panel reads its own reasons'
  // statuses (`unauditedMoverSources`): this page is a different set of
  // articles, and can carry no label at all while every mover rests on one.
  const unaudited = unauditedSources(data?.items ?? [])

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
    const { lookback: nextLookback, ...rest } = patch
    if (nextLookback !== undefined) onLookbackChange(nextLookback)
    setNarrowing({ ...narrowing, ...rest })
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
        <p className="mt-1 text-caption text-on-surface-variant">{SECTORS_PENDING_CAPTION}</p>
      ) : null}
      {data ? <UnauditedCaption sources={unaudited} /> : null}
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
                <Fragment key={item.id}>
                  <tr className="h-8 border-t border-outline/10 hover:bg-surface-container-low">
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
                      {/* No vendor filed the story here; the rules tier did,
                          from a company the headline names. Quiet, because it
                          is provenance, not a warning — but said, because the
                          vendor's tags and ours are different claims. */}
                      {item.attributedBy === 'headline' ? (
                        <span
                          title={`No source tagged this story to ${item.ticker}; the rules tier matched a company the headline names.`}
                          className="ml-1.5 text-caption font-normal text-on-surface-variant"
                        >
                          named in headline
                        </span>
                      ) : null}
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
                      <OtherLabelToggle
                        item={item}
                        open={expanded.has(item.id)}
                        onToggle={() => toggleExpanded(item.id)}
                      />
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
                  {expanded.has(item.id) && item.otherLabel !== null ? (
                    <tr>
                      <td />
                      <td colSpan={5} className={`${TD} pb-2`}>
                        <OtherLabelDetail item={item} />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
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
// Movers in the news — real since Phase 3 step 5 (decision 21)
// ---------------------------------------------------------------------- //

/** A direction, as a status pill. Direction is the one thing on this panel
 * `bullish`/`bearish` may colour; the word carries it too, for grayscale. */
function DirectionPill({ direction }: { direction: MoverDirection }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-label-sm ${
        direction === 'bullish'
          ? 'bg-bullish-container text-on-bullish-container'
          : 'bg-bearish-container text-on-bearish-container'
      }`}
    >
      {SENTIMENT_LABEL[direction]}
    </span>
  )
}

function MoverReasonLine({ reason }: { reason: NewsMoverReason }) {
  // Massive's own text is the reason, published as-is (decision 17); a
  // rules reason's evidence is the matched phrase, which the family name
  // already summarises, so it rides in the title.
  const quote = reason.source === 'massive' ? reason.evidence[0]?.reasoning ?? null : null
  const matched = reason.evidence.map((e) => e.reasoning).filter((r): r is string => r !== null)
  return (
    <li
      className="text-caption text-on-surface"
      title={reason.source === 'rules' && matched.length > 0 ? `Matched: ${matched.join(' · ')}` : undefined}
    >
      <span className="font-semibold">{moverReasonName(reason)}</span>{' '}
      <span className={SENTIMENT_CLASS[reason.direction]}>{SENTIMENT_LABEL[reason.direction]}</span>
      <span className="text-on-surface-variant">
        {' '}
        · <span className="font-mono tabular-nums">{reason.articleIds.length}</span>{' '}
        {reason.articleIds.length === 1 ? 'article' : 'articles'}
      </span>
      {quote ? <span className="text-on-surface-variant"> — &ldquo;{quote}&rdquo;</span> : null}
    </li>
  )
}

function MoverRow({
  mover,
  lookback,
  watching,
  failed,
  onWatch,
}: {
  mover: NewsMover
  /** Decides whether an article time needs its date (`moverArticleTime`). */
  lookback: NewsLookback
  watching: boolean
  failed: unknown
  onWatch: () => void
}) {
  const conflicting = mover.directions.length > 1
  return (
    <li className="py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-label-md text-on-surface">{mover.ticker}</span>
        <span className="text-caption text-on-surface-variant">{mover.sector}</span>
        {/* Side by side, never netted: a name flagged both ways is a name
            the news disagrees about, and one "mixed" chip would hide which
            reason says what. `role="group"` so the label is announced — an
            `aria-label` on a bare span is ignored by screen readers. */}
        <span
          className="flex items-center gap-1"
          role={conflicting ? 'group' : undefined}
          aria-label={
            conflicting
              ? `Reasons disagree: ${mover.directions.map((d) => SENTIMENT_LABEL[d]).join(' and ')}`
              : undefined
          }
        >
          {mover.directions.map((d) => (
            <DirectionPill key={d} direction={d} />
          ))}
        </span>
        {/* No confirm: a watch is additive and reversible, and a dialog on
            the safe direction trains the reflex that dismisses the one that
            matters. */}
        <button
          type="button"
          onClick={onWatch}
          disabled={watching}
          aria-label={`Watch ${mover.ticker}`}
          className="ml-auto rounded bg-primary px-2 py-0.5 text-label-sm text-on-primary disabled:opacity-50"
        >
          {watching ? 'Adding…' : 'Watch'}
        </button>
      </div>

      <ul aria-label={`Why ${mover.ticker} was flagged`} className="mt-1 space-y-0.5">
        {mover.reasons.map((r) => (
          <MoverReasonLine
            key={`${r.source}:${r.ruleId ?? ''}:${r.direction}`}
            reason={r}
          />
        ))}
      </ul>

      <p className="mt-1 text-caption text-on-surface-variant">
        ADV{' '}
        <span
          className="font-mono tabular-nums text-on-surface"
          title={`${formatInteger(mover.avgVolume)} shares a day`}
        >
          {formatCompactNumber(mover.avgVolume)}
        </span>{' '}
        over <span className="font-mono tabular-nums">{mover.advSessions}</span>{' '}
        {mover.advSessions === 1 ? 'session' : 'sessions'} · last close{' '}
        {/* The server's decimal string, verbatim — never parsed to a float.
            The title keeps the raw value reachable when it renders as —. */}
        <span className="font-mono tabular-nums text-on-surface" title={mover.lastClose}>
          {formatUsdDecimalString(mover.lastClose)}
        </span>{' '}
        · checked for {formatSessionDay(mover.tradeabilitySessionDate)}
      </p>

      <ul aria-label={`Articles on ${mover.ticker}`} className="mt-1 space-y-0.5">
        {mover.articles.map((a) => (
          <li key={a.id} className="truncate text-caption" title={a.headline}>
            {/* The only place a row says when its news ran. Under a
                multi-day lookback the clock alone would date a nine-day-old
                article to this morning. */}
            <span className="text-on-surface-variant">{moverArticleTime(a.time, lookback)} </span>
            <a
              href={a.url}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded text-on-surface hover:underline"
            >
              {a.headline}
            </a>
            {a.publisher !== null ? (
              <span className="uppercase text-on-surface-variant"> · {a.publisher}</span>
            ) : null}
          </li>
        ))}
      </ul>
      {mover.articleCount > mover.articles.length ? (
        <p className="text-caption text-on-surface-variant">
          and <span className="font-mono tabular-nums">{mover.articleCount - mover.articles.length}</span>{' '}
          more
        </p>
      ) : null}

      {failed ? (
        <div className="mt-1">
          <RequestFailed error={failed} what="the watch list" />
        </div>
      ) : null}
    </li>
  )
}

/** Why the list is empty, from the server's own counts. */
function NonePassed({ movers }: { movers: NewsMovers }) {
  const { signalled, failedTradeability, awaitingCheck, watchedExcluded } = movers
  const why =
    signalled === 0
      ? `No off-watch-list name carried a directional rules or vendor label${
          watchedExcluded > 0
            ? `; ${watchedExcluded} watched ${watchedExcluded === 1 ? 'name' : 'names'} did, and ${
                watchedExcluded === 1 ? 'is' : 'are'
              } in the feed instead`
            : ''
        }.`
      : `${signalled} signalled: ${failedTradeability} failed the tradeability filter${
          awaitingCheck > 0 ? `, ${awaitingCheck} awaiting a first check` : ''
        }.`
  // A stuck job is a degraded input, not a failed request: `caution`, never
  // `error`. Without this line "awaiting a first check" reads as a queue
  // that will drain, when nothing is draining it.
  const stuck = evaluationStuckLine(movers.evaluation)
  return (
    <div className="mt-3">
      <p className="text-body-sm text-on-surface">
        No off-watch-list name passed the filter {sincePhrase(movers.since)}.
      </p>
      <p className="mt-1 text-caption text-on-surface-variant">{why}</p>
      {stuck !== null ? <p className="mt-1 text-caption text-caution">{stuck}</p> : null}
    </div>
  )
}

function MoversInTheNews({ lookback }: { lookback: NewsLookback }) {
  const movers = useNewsMovers(lookback)
  const add = useAddWatch()
  const data = movers.data
  const empty = data ? moversEmptyState(data) : null
  // From the panel's own reasons, never the feed's page (Q22): the feed can
  // carry no label, or only the other source's, while these rows rest on
  // unaudited ones.
  const unaudited = unauditedMoverSources(data?.items ?? [])

  return (
    <section
      aria-labelledby="movers-heading"
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <h2 id="movers-heading" className="text-title-lg text-on-surface">
        Movers in the news
      </h2>
      <p className="mt-0.5 text-caption text-on-surface-variant">
        Flagged by news, not confirmed by price. Off-watch-list names, {LOOKBACK_PHRASE[lookback]}.
      </p>
      <UnauditedCaption sources={unaudited} />
      {data && !data.sectorsAvailable ? (
        <p className="mt-1 text-caption text-on-surface-variant">{SECTORS_PENDING_CAPTION}</p>
      ) : null}

      {movers.isError && data ? (
        // A failed refetch over rows already on screen keeps them, and says
        // what they are now: the last answer, not the current one.
        <div className="mt-2">
          <RequestFailed error={movers.error} what="the movers list" />
          <p className="mt-0.5 text-caption text-on-surface-variant">
            These rows may be stale — last evaluated {formatDateTimeET(data.evaluatedAt)} ET.
          </p>
        </div>
      ) : null}

      {movers.isPending ? (
        <div className="mt-3">
          <TableSkeleton rows={4} columns={2} label="Loading movers in the news" />
        </div>
      ) : !data ? (
        <div className="mt-3">
          <RequestFailed error={movers.error} what="the movers list" />
        </div>
      ) : (
        <>
          {data.feedsStale ? (
            // Stale is a degraded input, not a failed request: `caution`,
            // the same reason Unclassified is never `error`.
            <p className="mt-3 text-body-sm text-caution">
              {staleLine(data)}
              {empty === null ? ' — this list may be missing names.' : '.'}
            </p>
          ) : null}
          {empty === 'none-passed' ? <NonePassed movers={data} /> : null}
          {empty === 'stale' ? (
            <p className="mt-1 text-caption text-on-surface-variant">
              An empty list says nothing while a feed is down: the names that would have passed
              may not have arrived.
            </p>
          ) : null}
          {empty === null ? (
            <ol aria-label="Movers" className="mt-1 divide-y divide-outline-variant">
              {/* The server's order, exactly. Rules rows rank first and the
                  client never re-ranks — the one-sort rule markets.ts keeps. */}
              {data.items.map((m) => (
                <MoverRow
                  key={m.ticker}
                  mover={m}
                  lookback={lookback}
                  watching={add.isPending && add.variables === m.ticker}
                  failed={add.isError && add.variables === m.ticker ? add.error : null}
                  onWatch={() => add.mutate(m.ticker)}
                />
              ))}
            </ol>
          ) : null}
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
          ? `${list.manualCount} of ${list.cap} manual watches`
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
              The SPDR holdings seed is not built, so sector leaders are not watched yet — they
              join when it is. The manual-watch cap is unaffected.
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

export function News() {
  // The feed is the only live thing on this page, so the freshness pill
  // reports the feed's last successful read and nothing else. The rail's
  // panels refresh daily or monthly, or are still fixtures.
  const [feedAt, setFeedAt] = useState<string | null>(null)
  const onFeedUpdated = useCallback((at: string | null) => setFeedAt(at), [])
  // One lookback for the feed and the movers panel (decision 21: the panel
  // follows the page's control).
  const [lookback, setLookback] = useState<NewsLookback>(DEFAULT_NEWS_FILTER.lookback)

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
          {/* The movers panel sits beside the feed from `2xl` up and stacks
              under it below that — the feed's headline column is the one
              that cannot give up width. */}
          <div className="grid items-start gap-6 2xl:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
            <LiveFeed
              lookback={lookback}
              onLookbackChange={setLookback}
              onUpdatedAt={onFeedUpdated}
            />
            <MoversInTheNews lookback={lookback} />
          </div>
          <MarketCalendar />
        </div>
      </div>
    </div>
  )
}
