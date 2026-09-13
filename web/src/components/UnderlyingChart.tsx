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
  quoteSeries,
  seriesChange,
  seriesSpansDays,
  windowForRange,
  type ChartSeries,
} from '../lib/api'
import { formatPct, formatUsd, signClass } from '../lib/format'

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

  // The move over the window on screen, not over the day. A range control
  // that redraws the axis but leaves a daily figure beside it is reporting
  // on a chart nobody is looking at.
  const windowChange = useMemo(
    () => seriesChange(points, 0, points.length - 1),
    [points],
  )

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
              no figure. */}
          {stale ? (
            <span role="status" className="text-label-md text-on-surface-variant">
              Reading {range}…
            </span>
          ) : (
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
   fields are empty. */
