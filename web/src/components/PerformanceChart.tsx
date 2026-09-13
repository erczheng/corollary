import { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { type ChartRange } from '../lib/types'
import {
  formatSeriesKey,
  seriesChange,
  seriesSpansDays,
  type ChartSeries,
  type SeriesPoint,
} from '../lib/api'
import { RequestFailed } from './RequestFailed'
import { TableSkeleton } from './Skeleton'
import { formatDateTimeET, formatPct, formatUsd, signClass } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

interface PerformanceChartProps {
  /** The series to plot, with the resolution the server actually served.
   *
   * Passed in rather than read from a module constant because Paper and Cash
   * are different accounts with different balances — the chart follows the
   * account toggle. It is Alpaca's own equity curve, shaped by the
   * Dashboard's `equityCurve`, and its resolution comes from the
   * `timeframe` the response echoed back rather than from the one that was
   * asked for. */
  series: ChartSeries
  /** The selected range. **Controlled**: the button drives the *request*,
   * and the request is made by the page that owns the query, so the state
   * cannot live in here. Slicing one fixed series instead was the defect —
   * `1D` drew a single point. */
  range: ChartRange
  onRangeChange: (range: ChartRange) => void
  /** First load, with nothing yet to draw. */
  isPending?: boolean
  isError?: boolean
  error?: unknown
  /** A newer window is in flight and what is drawn is still the previous
   * one. The chart stays up rather than blanking — an empty plot between
   * two ranges claims there is no data — but it is dimmed and says so. */
  stale?: boolean
  /** Which book this is, for the wording when there is nothing to draw. */
  accountLabel?: string
  /** When Corollary first ran, as an ISO instant, or null if that is not
   * recorded.
   *
   * The footer needs it to say whose history this is. **`pointsBeforeT0`
   * from the same response is deliberately not used**: it counts points in
   * the *server's* window, while the Dashboard drops the pre-funding zero
   * pad, so the number would be quoted against a different set of points
   * than the one on screen. Comparing the first plotted point with t₀
   * answers the same question about whatever is actually drawn, in
   * whichever range is selected. */
  t0?: string | null
}

/** Recharts types this as `number | string | undefined` because some chart
 * families index by category. On a line chart it is always the array
 * index, but it is worth failing closed rather than trusting that. */
function toIndex(active: unknown, length: number): number | null {
  const i = typeof active === 'number' ? active : Number(active)
  return Number.isInteger(i) && i >= 0 && i < length ? i : null
}

export function PerformanceChart({
  series,
  range,
  onRangeChange,
  isPending = false,
  isError = false,
  error = null,
  stale = false,
  accountLabel = 'this account',
  t0 = null,
}: PerformanceChartProps) {
  // Drag-to-measure: press on the chart and sweep to read the change over
  // that window. Deliberately transient — it clears on release rather than
  // becoming a mode you can leave the chart in and later misread.
  const [dragFrom, setDragFrom] = useState<number | null>(null)
  const [dragTo, setDragTo] = useState<number | null>(null)

  const { resolution, points } = series

  const selection =
    dragFrom !== null && dragTo !== null ? seriesChange(points, dragFrom, dragTo) : null

  const clearDrag = () => {
    setDragFrom(null)
    setDragTo(null)
  }

  // The release that ends a drag often lands outside the plot area — off
  // the edge of the chart, or off the window entirely. Listening on the
  // window is what makes "it goes back to normal when I let go" true
  // everywhere, rather than only when you release over the chart.
  useEffect(() => {
    if (dragFrom === null) return
    window.addEventListener('mouseup', clearDrag)
    return () => window.removeEventListener('mouseup', clearDrag)
  }, [dragFrom])

  // Changing range mid-drag would leave indices pointing into a series
  // that no longer exists.
  const selectRange = (r: ChartRange) => {
    clearDrag()
    onRangeChange(r)
  }

  const dragging = dragFrom !== null

  // One ET day of intraday bars needs no date on every tick; a week of them
  // does, or the same clock time appears once per session with nothing to
  // tell them apart.
  const compactTicks = resolution === 'intraday' && !seriesSpansDays(points)
  const tickLabel = useMemo(
    () => (key: string) => formatSeriesKey(resolution ?? 'daily', key, { compact: compactTicks }),
    [resolution, compactTicks],
  )
  const pointLabel = (key: string) =>
    resolution === 'intraday'
      ? `${formatSeriesKey('intraday', key)} ET`
      : formatSeriesKey('daily', key)

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex rounded-full border border-outline bg-surface-container-low p-1 text-label-md">
          {RANGES.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => selectRange(r)}
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
        {stale && (
          <p role="status" className="text-caption text-on-surface-variant">
            Reading {range}…
          </p>
        )}
        {/* "Compare to SPY" used to sit here, drawing `BENCHMARK_HISTORY` —
            a seeded random walk based at $25,000 — against what is now the
            account's real equity. Against a real curve that is an invented
            line at the wrong scale, which is the thing PRD §8.5 rules out.
            The honest version reads SPY from `useUnderlyings(['SPY'])` and
            indexes it to the equity at the start of the window; it is a
            feature, not a checkbox, and it is not in this phase. */}
      </div>

      {/* Every state below keeps the range control above it mounted. An
          error or an empty window that replaced the whole panel would leave
          no way back to a range that works. */}
      {isPending ? (
        <div className="mt-3">
          <TableSkeleton rows={4} columns={1} label="Loading the equity curve" />
        </div>
      ) : isError ? (
        <div className="mt-3">
          <RequestFailed error={error} what="the equity curve" />
        </div>
      ) : points.length < 2 ? (
        <p className="mt-3 max-w-prose text-body-md text-on-surface-variant">
          {points.length === 0
            ? `No equity history for ${accountLabel} in this window. The broker reports the curve once the account has been funded and a session has run inside the range you asked for.`
            : resolution === 'intraday'
              ? `Only one bar of equity history for ${accountLabel} in this window — a curve needs two points to have a shape. It fills in as the session runs.`
              : `Only one day of equity history for ${accountLabel} so far — a curve needs two closes to have a shape. It fills in as sessions close.`}
        </p>
      ) : (
        <Plot
          points={points}
          stale={stale}
          dragging={dragging}
          selection={selection}
          tickLabel={tickLabel}
          pointLabel={pointLabel}
          onDragStart={(i) => {
            setDragFrom(i)
            setDragTo(i)
          }}
          onDragMove={(i) => {
            if (dragFrom !== null) setDragTo(i)
          }}
        />
      )}

      {points.length > 0 && !isPending && !isError && (
        /* Whose history this is, in words.
         *
         * This line used to read "Since <first point> — Corollary's first
         * run", which was true of a fixture generated at Corollary's start
         * and is **false** of Alpaca's curve: the first point is the
         * *broker's*, and on a funded account most of it predates the engine
         * entirely. Spec decision 6 marks t₀ for exactly this reason — the
         * chart must not claim credit for manual trading.
         *
         * The comparison is date against date: an intraday key is an
         * instant, so its UTC date is taken before it meets t₀'s. */
        <p className="mt-6 max-w-prose text-caption text-on-surface-variant">
          The broker's own equity curve, from {pointLabel(points[0].key)}.{' '}
          {t0 === null
            ? 'Corollary’s first run is not recorded, so none of this is attributed to the engine.'
            : points[0].key.slice(0, 10) < t0.slice(0, 10)
              ? `Part of this window predates Corollary’s first run on ${formatDateTimeET(t0)} ET — it is the account’s history, not the engine’s record.`
              : `Entirely since Corollary’s first run on ${formatDateTimeET(t0)} ET.`}{' '}
          Nothing is reconstructed. Drag across the chart to measure a period.
        </p>
      )}
    </div>
  )
}

function Plot({
  points,
  stale,
  dragging,
  selection,
  tickLabel,
  pointLabel,
  onDragStart,
  onDragMove,
}: {
  points: SeriesPoint[]
  stale: boolean
  dragging: boolean
  selection: ReturnType<typeof seriesChange>
  tickLabel: (key: string) => string
  pointLabel: (key: string) => string
  onDragStart: (index: number) => void
  onDragMove: (index: number) => void
}) {
  return (
    /* select-none unconditionally, not only while dragging: the browser
       begins its selection on the same mousedown that starts the drag, so a
       class applied on the next render is already too late and the first
       sweep paints a highlight over the whole plot. There is no text here
       worth selecting anyway. */
    <div
      className={`relative mt-3 h-72 w-full select-none transition-opacity duration-base ease-standard ${
        stale ? 'opacity-50' : ''
      }`}
      aria-busy={stale}
    >
      {selection && (
        <div
          role="status"
          aria-live="polite"
          className="pointer-events-none absolute left-2 top-1 z-10 rounded-lg border border-outline-warm bg-surface-container-lowest px-3 py-2"
        >
          <p className="text-caption text-on-surface-variant">
            {pointLabel(selection.from.key)} → {pointLabel(selection.to.key)}
          </p>
          {/* This one really is P&L, so bullish/bearish is the right
              pair here — unlike a deposit, which is money moved rather
              than money made. */}
          <p className={`text-data-md ${signClass(selection.change)}`}>
            {formatUsd(selection.change, { signed: true })}
            <span className="ml-2">{formatPct(selection.changePct, { signed: true })}</span>
          </p>
        </div>
      )}

      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={points}
          margin={{ top: 8, right: 8, left: 8, bottom: 8 }}
          onMouseDown={(s, e) => {
            // Belt and braces with select-none above: preventDefault stops
            // the drag-selection from ever starting, which also keeps the
            // cursor from turning into a text caret mid-sweep.
            e?.preventDefault()
            const i = toIndex(s?.activeTooltipIndex, points.length)
            if (i === null) return
            onDragStart(i)
          }}
          onMouseMove={(s) => {
            const i = toIndex(s?.activeTooltipIndex, points.length)
            if (i !== null) onDragMove(i)
          }}
        >
          <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
          <XAxis
            // Keyed by the raw key — the ISO date or instant, not the
            // display label: "Aug 8" occurs twice in a 1Y window, and a
            // duplicated category value makes the selection band ambiguous
            // about which one it means. Intraday it is worse still, since
            // every bar in an hour would share one label.
            dataKey="key"
            tickFormatter={tickLabel}
            tick={{ fill: 'var(--on-surface-variant)', fontSize: 12 }}
            tickLine={false}
            axisLine={{ stroke: 'var(--outline-warm)' }}
            tickMargin={16}
            minTickGap={40}
          />
          <YAxis
            tick={{ fill: 'var(--on-surface-variant)', fontSize: 12 }}
            tickLine={false}
            axisLine={false}
            tickMargin={12}
            width={84}
            tickFormatter={(v: number) => formatUsd(v)}
            domain={['auto', 'auto']}
          />
          {/* The hover tooltip and the drag readout answer different
              questions and would sit on top of each other, so only one
              is up at a time. */}
          {!dragging && (
            <Tooltip
              contentStyle={{
                background: 'var(--surface-container-lowest)',
                border: '1px solid var(--outline-warm)',
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: 'var(--on-surface-variant)' }}
              labelFormatter={(d) => pointLabel(String(d))}
              formatter={(value) => [
                formatUsd(typeof value === 'number' ? value : Number(value)),
                'Portfolio',
              ]}
            />
          )}
          <Line
            type="monotone"
            dataKey="value"
            stroke="var(--primary)"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          {selection && (
            <ReferenceArea
              x1={selection.from.key}
              x2={selection.to.key}
              fill="var(--primary)"
              fillOpacity={0.12}
              stroke="var(--primary)"
              strokeOpacity={0.35}
            />
          )}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
