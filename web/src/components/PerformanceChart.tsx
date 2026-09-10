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
import { BENCHMARK_HISTORY, sliceRange } from '../lib/mockData'
import { type ChartRange, type PricePoint } from '../lib/types'
import { rangeChange } from '../lib/chart'
import { formatDateOnly, formatPct, formatUsd, signClass } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

interface PerformanceChartProps {
  /** The series to plot. Passed in rather than read from a module constant
   * because Paper and Cash are different accounts with different balances
   * — the chart follows the account toggle. */
  history: PricePoint[]
}

function toChartDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-US', {
    timeZone: 'UTC',
    month: 'short',
    day: 'numeric',
  })
}

/** Recharts types this as `number | string | undefined` because some chart
 * families index by category. On a line chart it is always the array
 * index, but it is worth failing closed rather than trusting that. */
function toIndex(active: unknown, length: number): number | null {
  const i = typeof active === 'number' ? active : Number(active)
  return Number.isInteger(i) && i >= 0 && i < length ? i : null
}

export function PerformanceChart({ history }: PerformanceChartProps) {
  const [range, setRange] = useState<ChartRange>('3M')
  const [showBenchmark, setShowBenchmark] = useState(false)
  // Drag-to-measure: press on the chart and sweep to read the change over
  // that window. Deliberately transient — it clears on release rather than
  // becoming a mode you can leave the chart in and later misread.
  const [dragFrom, setDragFrom] = useState<number | null>(null)
  const [dragTo, setDragTo] = useState<number | null>(null)

  const { portfolio, data } = useMemo(() => {
    const sliced = sliceRange(history, range)
    const benchmark = sliceRange(BENCHMARK_HISTORY, range)
    return {
      portfolio: sliced,
      data: sliced.map((p, i) => ({
        // Keyed by the ISO date, not the display label: "Aug 8" occurs
        // twice in a 1Y window, and a duplicated category value makes the
        // selection band ambiguous about which one it means.
        date: p.date,
        portfolio: p.value,
        benchmark: benchmark[i]?.value,
      })),
    }
  }, [history, range])

  const selection =
    dragFrom !== null && dragTo !== null ? rangeChange(portfolio, dragFrom, dragTo) : null

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
    setRange(r)
  }

  const dragging = dragFrom !== null

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
        <label className="flex items-center gap-2 text-label-md text-on-surface-variant">
          <input
            type="checkbox"
            checked={showBenchmark}
            onChange={(e) => setShowBenchmark(e.target.checked)}
            className="accent-primary"
          />
          Compare to SPY
        </label>
      </div>

      {/* select-none unconditionally, not only while dragging: the browser
          begins its selection on the same mousedown that starts the drag,
          so a class applied on the next render is already too late and the
          first sweep paints a highlight over the whole plot. There is no
          text here worth selecting anyway. */}
      <div className="relative mt-3 h-72 w-full select-none">
        {selection && (
          <div
            role="status"
            aria-live="polite"
            className="pointer-events-none absolute left-2 top-1 z-10 rounded-lg border border-outline-warm bg-surface-container-lowest px-3 py-2"
          >
            <p className="text-caption text-on-surface-variant">
              {formatDateOnly(selection.from.date)} → {formatDateOnly(selection.to.date)}
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
            data={data}
            margin={{ top: 8, right: 8, left: 8, bottom: 8 }}
            onMouseDown={(s, e) => {
              // Belt and braces with select-none above: preventDefault stops
              // the drag-selection from ever starting, which also keeps the
              // cursor from turning into a text caret mid-sweep.
              e?.preventDefault()
              const i = toIndex(s?.activeTooltipIndex, data.length)
              if (i === null) return
              setDragFrom(i)
              setDragTo(i)
            }}
            onMouseMove={(s) => {
              if (dragFrom === null) return
              const i = toIndex(s?.activeTooltipIndex, data.length)
              if (i !== null) setDragTo(i)
            }}
          >
            <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={(d: string) => toChartDate(d)}
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
                labelFormatter={(d) => formatDateOnly(String(d))}
                formatter={(value, name) => [
                  formatUsd(typeof value === 'number' ? value : Number(value)),
                  name === 'portfolio' ? 'Portfolio' : 'SPY',
                ]}
              />
            )}
            <Line
              type="monotone"
              dataKey="portfolio"
              stroke="var(--primary)"
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
            {showBenchmark && (
              <Line
                type="monotone"
                dataKey="benchmark"
                stroke="var(--accent)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            )}
            {selection && (
              <ReferenceArea
                x1={selection.from.date}
                x2={selection.to.date}
                fill="var(--primary)"
                fillOpacity={0.12}
                stroke="var(--primary)"
                strokeOpacity={0.35}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {portfolio.length > 0 && (
        <p className="mt-6 text-caption text-on-surface-variant">
          Since {formatDateOnly(history[0].date)} — Corollary's first run. No
          pre-Corollary history is reconstructed. Drag across the chart to measure
          a period.
        </p>
      )}
    </div>
  )
}
