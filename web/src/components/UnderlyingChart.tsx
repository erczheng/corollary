import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { type ChartRange } from '../lib/types'
import { useUnderlyings } from '../lib/queries'
import { RequestFailed } from './RequestFailed'
import {
  formatSeriesKey,
  isInvalidSeriesWindow,
  latestSession,
  quoteSeries,
  resolutionForTimeframe,
  seriesChange,
  seriesSpansDays,
  windowForRange,
  type ChartSeries,
} from '../lib/api'
import { formatPct, formatSessionDay, formatUsd, signClass } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

const AXIS_TICK = { fill: 'var(--on-surface-variant)', fontSize: 12 }

const NO_SERIES: ChartSeries = { resolution: null, points: [] }

/** A stock's price over a window you choose.
 *
 * **The range control drives the request, not a slice.** It used to ask for
 * one 400-session daily series and cut it up, which meant `1D` drew a single
 * point and `1W` drew about five — the buttons offered a resolution the data
 * never had. Each range now resolves to a `period` and a `timeframe`
 * (`windowForRange`), both go in the query key, and the day comes back at
 * five-minute bars.
 *
 * **Which resolution answered is read off the response**, never inferred
 * from what was asked: `quoteSeries` reports `intraday` or `daily` by which
 * of the quote's two series fields is populated, and `null` when neither is
 * — a window that held no session, which is what `1D` looks like on a
 * Sunday. That is a designed state here, not an empty chart and not a flat
 * line.
 *
 * What *is* compared against the request is whether the answer answers it,
 * and there are **two separate ways it can fail to**, because one detector
 * only sees two of the seven buttons:
 *
 * - **The served resolution is not the requested one** — daily closes to a
 *   `1D`/`5Min` request. Only ever a mismatch when the request was
 *   intraday, so it is blind on `1M`/`3M`/`YTD`/`1Y`/`All`, which all ask
 *   for `1D` bars and would accept a daily answer from any server.
 * - **The response omitted a series field** (`quoteSeries`' `missingField`)
 *   — the live skew, and the one the five daily ranges need: a server
 *   predating the split answers `history` with no `intraday` key and its
 *   own default 400 closes, which under `3M` draws cleanly and measures a
 *   year.
 *
 * Either way the series that *did* arrive is still drawn, and only the two
 * readouts that describe the **window** are withheld — the change figure
 * and the session day — with an alert naming which of the two causes it
 * was. Withheld, not caveated: a figure labelled `over 3M` measuring 400
 * calendar days is worse than no figure. The served resolution still
 * decides how every key reads; that is a different operation.
 *
 * The range control stays mounted through every one of those states. A
 * refused window that replaced the whole panel with an error would leave no
 * way back to a range that works.
 *
 * It carries no strike and no contract. It sat in the option ticket first
 * and that was the wrong home — a chart is a thing you *browse*, and the
 * place you browse stocks is the stock table. What the ticket actually
 * needed from it was one sentence about where spot sits against the strike,
 * which is now stated there directly.
 */
export function UnderlyingChart({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<ChartRange>('3M')
  const seriesWindow = useMemo(() => windowForRange(range), [range])
  const query = useUnderlyings([symbol], seriesWindow)
  const quote = query.data?.find((u) => u.symbol === symbol) ?? null

  const series = useMemo(() => (quote ? quoteSeries(quote) : NO_SERIES), [quote])
  const { resolution, points } = series

  // True while the previous window is still the thing on screen. The chart
  // is kept rather than blanked (`placeholderData` in `useUnderlyings`),
  // because an empty plot between two ranges reads as "there is no data" —
  // but anything that *describes* the window has to wait for the window it
  // describes.
  const stale = query.isPlaceholderData

  // Did the server answer the question that was asked?
  //
  // This infers nothing. The *served* resolution stays authoritative for
  // every decision that reads one — `tickLabel`, `pointLabel`,
  // `compactTicks` — and this asks the separate question of whether the
  // answer is an answer to *this* request. A server that ignores `period`
  // and `timeframe` returns its default daily series to a `1D`/`5Min`
  // request: 400 dated bars under a button that says intraday, with a
  // change figure labelled "over 1D" measuring a year. Before the crash
  // fix that payload threw and the page blanked, which was at least
  // unmistakable; drawn, it looks fine and is wrong.
  //
  // `/api/markets/underlyings` echoes no `timeframe` the way
  // `/api/account/history` does, so requested-against-served is the only
  // form this question can take here. A false positive would itself be
  // worth surfacing: `1D` asked at `5Min` and answered as daily closes is
  // not something a correct server produces.
  //
  // Not raised while the previous window is still on screen — a daily
  // series under a freshly picked 5Min timeframe is exactly what
  // `placeholderData` is for, and it settles on the next paint.
  const resolutionSkew =
    !stale &&
    resolution !== null &&
    resolution !== resolutionForTimeframe(seriesWindow.timeframe)

  // The other half of the same question, and the half that covers the five
  // daily ranges. `resolutionSkew` can only fire on `1D` and `1W`: every
  // other button asks for `1D` bars, so a stale server's daily closes are
  // a resolution match and sail straight through. What that server *also*
  // does is omit the `intraday` key, and a build that does not carry a
  // field this page reads cannot be taken to have honoured `period`
  // either — the 400 closes it returns are its default window, not the one
  // that was asked for.
  //
  // Gated on there being something drawn: with no points this is the
  // nothing-arrived state, which `ChartBody` states in full and in place
  // of a chart. Raising the alert there too would say it twice.
  const shapeSkew = !stale && series.missingField === true && points.length > 0

  const skewed = resolutionSkew || shapeSkew

  // The move over the window on screen, not over the day. A range control
  // that redraws the axis but leaves a daily figure beside it is reporting
  // on a chart nobody is looking at.
  const windowChange = useMemo(
    () => seriesChange(points, 0, points.length - 1),
    [points],
  )

  // Which day the series ends on, named only when that day is not today's.
  // `1D` on a Saturday draws Friday: the right data — there is no session
  // today — but a control reading `1D` over it reads as today. The test is
  // the newest point against today's ET date, never a market calendar and
  // never a bare `new Date()`, whose day is the browser's.
  //
  // Withheld on the same terms as the change readout beside it: while the
  // previous window is still drawn, and wherever there is no line, since
  // the no-session and single-bar states already say more than this would.
  const session = latestSession(series)
  const sessionLead =
    stale || skewed || query.isPending || query.isError || points.length < 2
      ? null
      : (session?.lead ?? null)

  // One ET day of five-minute bars needs no date on every tick; a week of
  // them does, or `3:45 PM` appears four times meaning four different days.
  const compactTicks = resolution === 'intraday' && !seriesSpansDays(points)
  const tickLabel = (key: string) =>
    formatSeriesKey(resolution ?? 'daily', key, { compact: compactTicks })
  const pointLabel = (key: string) =>
    resolution === 'intraday'
      ? `${formatSeriesKey('intraday', key)} ET`
      : formatSeriesKey('daily', key)

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div
          role="group"
          aria-label="Chart range"
          className="inline-flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md"
        >
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRange(r)}
              aria-pressed={range === r}
              className={
                range === r
                  ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
                  : 'rounded-full px-3 py-1 text-on-surface-variant transition-colors duration-base ease-standard hover:text-on-surface'
              }
            >
              {r}
            </button>
          ))}
        </div>

        <p className="flex items-center gap-3 text-label-md text-on-surface-variant">
          <span>
            {symbol}{' '}
            {quote && <span className="text-data-md text-on-surface">{formatUsd(quote.price)}</span>}
          </span>
          {/* Withheld while the previous window is still drawn: a figure
              labelled "over 1W" measured across three months is worse than
              no figure. Withheld on the same terms when the server answered
              at a resolution nobody asked for — the same sentence, with a
              version skew as the cause instead of a pending refetch, and
              the case that actually occurs. */}
          {stale ? (
            <span role="status" className="text-label-md text-on-surface-variant">
              Reading {range}…
            </span>
          ) : (
            !skewed &&
            windowChange && (
              <span className={`text-data-md ${signClass(windowChange.change)}`}>
                {formatUsd(windowChange.change, { signed: true })}{' '}
                {formatPct(windowChange.changePct, { signed: true })}
                <span className="ml-1 text-label-md text-on-surface-variant">over {range}</span>
              </span>
            )
          )}
        </p>
      </div>

      {/* Stated, not caveated: the figure and the day label are both gone
          above, and this says why they are. `error` rather than `bearish`
          or `caution` — a response that does not answer the request is a
          fault between this page and the server, not a loss and not a
          market condition. */}
      {skewed && (
        <p role="alert" className="mb-2 max-w-prose text-caption text-error">
          {/* Two causes, one consequence. The cause is named because the
              two are fixed by different things — one is a server ignoring
              the query, the other a server that predates the field — and
              "something is off with this chart" is not a bug report
              anybody can act on. */}
          {shapeSkew ? (
            <>
              The quote for {symbol} left out one of the two price series fields this page
              reads, so the running API and this build disagree about the shape of a quote. A
              server missing that field cannot be taken to have honoured the {range} window it
              was sent either, so the change over the window and the session day are withheld
              rather than printed over a window nobody can confirm was served.
            </>
          ) : (
            <>
              Asked for {symbol} over {range} at {seriesWindow.timeframe} bars; the server
              answered with {resolution === 'daily' ? 'daily closes' : 'intraday bars'}. The
              change over the window is withheld rather than printed with a caveat, because it
              would measure a window nobody asked for.
            </>
          )}{' '}
          What is drawn below is the series that did arrive, at the resolution it arrived at.
        </p>
      )}

      {/* Informational, so `on-surface-variant` and not `caution` — an
          earlier session is not a fault and not a loss. The day itself is
          mono and tabular like every other figure on the page. */}
      {sessionLead !== null && session !== null && (
        <p className="mb-2 text-caption text-on-surface-variant">
          {sessionLead} — <span className="text-data-sm">{formatSessionDay(session.date)}</span>
        </p>
      )}

      <ChartBody
        symbol={symbol}
        range={range}
        timeframe={seriesWindow.timeframe}
        series={series}
        stale={stale}
        isPending={query.isPending}
        isError={query.isError}
        error={query.error}
        hasQuote={quote !== null}
        previousClose={quote?.previousClose ?? null}
        tickLabel={tickLabel}
        pointLabel={pointLabel}
      />
    </div>
  )
}

function ChartBody({
  symbol,
  range,
  timeframe,
  series,
  stale,
  isPending,
  isError,
  error,
  hasQuote,
  previousClose,
  tickLabel,
  pointLabel,
}: {
  symbol: string
  range: ChartRange
  timeframe: string
  series: ChartSeries
  stale: boolean
  isPending: boolean
  isError: boolean
  error: unknown
  hasQuote: boolean
  previousClose: number | null
  tickLabel: (key: string) => string
  pointLabel: (key: string) => string
}) {
  if (isError) {
    return (
      <>
        <RequestFailed error={error} what={`the ${symbol} price history`} />
        {/* The server's own message names the finest timeframe that would
            have fit, so it is rendered verbatim above rather than reworded.
            This line only says which kind of failure it was: a window past
            the point ceiling is a request that cannot be served, not a feed
            that has gone down. */}
        {isInvalidSeriesWindow(error) && (
          <p className="mt-2 max-w-prose text-caption text-on-surface-variant">
            Nothing is wrong with the feed — that window is more points than the server will
            return at once. Pick a shorter range above.
          </p>
        )}
      </>
    )
  }

  if (isPending) {
    return (
      <p role="status" className="text-caption text-on-surface-variant">
        Reading {symbol} over {range} at {timeframe} bars…
      </p>
    )
  }

  if (!hasQuote) {
    return (
      <p className="text-caption text-on-surface-variant">
        The server quoted no price for {symbol}, so there is no series to draw. A symbol with no
        price is left out of the response rather than served as a row of zeroes.
      </p>
    )
  }

  // `resolution` is not read here: what it decides is how a key reads, and
  // that arrives already resolved, as `tickLabel` and `pointLabel`.
  const { points } = series

  // A series field was missing **and nothing drawable arrived** — which is
  // a different thing from both fields being empty, and so gets different
  // words. Both empty is the server saying the window held no session.
  // Neither present is this page and the API disagreeing about the shape
  // of a quote, and captioning that "no session" would report a quiet
  // Sunday for a version skew. `error` rather than `bearish`: a response
  // this client cannot read is a fault, not a loss.
  //
  // `points.length === 0` is load-bearing. `missingField` is now also true
  // when one field was absent and the *other* came back full — 400 daily
  // closes and no `intraday` key — and this paragraph would be flatly
  // false over a drawable series, claiming no price series above a drawn
  // one. That case takes the withholding path instead: the caller draws
  // the bars, drops the window figure and the session lead, and says why
  // in the skew alert above.
  if (series.missingField === true && points.length === 0) {
    return (
      <p role="alert" className="max-w-prose text-caption text-error">
        The quote for {symbol} carried no price series — neither daily closes nor intraday
        bars. This page and the API it is talking to disagree about the shape of a quote, so
        nothing is drawn rather than a flat line at the last price. Reload once the server has
        been restarted on the current build.
      </p>
    )
  }

  // Neither series field came back. The market did not open inside this
  // window — `1D` on a Sunday, or a holiday Monday — and the honest answer
  // is to say so. A flat line at the last price would claim a session that
  // did not happen.
  if (points.length === 0) {
    return (
      <p className="max-w-prose text-caption text-on-surface-variant">
        No session for {symbol} in this window. {range} covers no trading day right now — try a
        longer range.
      </p>
    )
  }

  if (points.length === 1) {
    return (
      <p className="max-w-prose text-caption text-on-surface-variant">
        One {timeframe} bar for {symbol} so far in this window, so there is no line to draw yet.
        It fills in as the session runs.
      </p>
    )
  }

  return (
    <div
      className={`h-48 transition-opacity duration-base ease-standard ${stale ? 'opacity-50' : ''}`}
      aria-busy={stale}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
          <XAxis
            // The raw key, daily or intraday — formatted for display but
            // never used as the category itself. Two five-minute bars an
            // hour apart share a display label and would collapse onto one x.
            dataKey="key"
            tickFormatter={tickLabel}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: 'var(--outline-warm)' }}
            minTickGap={40}
          />
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            width={72}
            // Padded rather than 'auto', which pins the series to the
            // very top and bottom of the plot and leaves the line
            // touching the axis.
            domain={[(min: number) => min * 0.99, (max: number) => max * 1.01]}
            tickFormatter={(v: number) => formatUsd(v)}
          />
          <Tooltip
            contentStyle={{
              background: 'var(--surface-container-lowest)',
              border: '1px solid var(--outline-warm)',
              borderRadius: 8,
              color: 'var(--on-surface)',
            }}
            labelFormatter={(d) => pointLabel(String(d))}
            formatter={(value) => [formatUsd(Number(value)), symbol]}
          />
          {/* Yesterday's close, so the day's move is the distance from
              this line rather than something to work out. */}
          {/* Absent on a name with no prior session — the line is then
              omitted rather than drawn at zero, which would compress the
              whole axis to make room for a price that never happened. */}
          {previousClose !== null && (
            <ReferenceLine
              y={previousClose}
              stroke="var(--outline)"
              strokeDasharray="4 4"
              label={{
                value: 'Prev close',
                position: 'insideTopLeft',
                fill: 'var(--on-surface-variant)',
                fontSize: 12,
              }}
            />
          )}
          {/* The last point is the live price, appended past the vendor's
              15-minute embargo at every timeframe. It is not an outlier and
              is never trimmed. */}
          <Line
            type="monotone"
            dataKey="value"
            stroke="var(--primary)"
            strokeWidth={2}
            dot={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

/* A note for whoever adds the next chart: the intraday/daily split is read
   from the response and nothing here guesses it. `resolution === null` means
   the server stated no resolution, which only happens when both series
   fields came back with nothing in them — empty, absent, or one of each.
   Whether a field was *absent* is the separate `missingField`, and it is
   set on a populated response too. */
