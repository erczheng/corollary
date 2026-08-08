import { useMemo, useState } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  BENCHMARK_HISTORY,
  PORTFOLIO_HISTORY,
  sliceRange,
  type ChartRange,
} from '../lib/mockData'
import { formatDateET, formatUsd } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

function toChartDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-US', {
    timeZone: 'UTC',
    month: 'short',
    day: 'numeric',
  })
}

export function PerformanceChart() {
  const [range, setRange] = useState<ChartRange>('3M')
  const [showBenchmark, setShowBenchmark] = useState(false)

  const data = useMemo(() => {
    const portfolio = sliceRange(PORTFOLIO_HISTORY, range)
    const benchmark = sliceRange(BENCHMARK_HISTORY, range)
    return portfolio.map((p, i) => ({
      date: p.date,
      label: toChartDate(p.date),
      portfolio: p.value,
      benchmark: benchmark[i]?.value,
    }))
  }, [range])

  const startDate = PORTFOLIO_HISTORY[0]?.date

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md">
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

      <div className="mt-3 h-72 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
            <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
            <XAxis
              dataKey="label"
              tick={{ fill: 'var(--on-surface-variant)', fontSize: 12 }}
              tickLine={false}
              axisLine={{ stroke: 'var(--outline-warm)' }}
              minTickGap={40}
            />
            <YAxis
              tick={{ fill: 'var(--on-surface-variant)', fontSize: 12 }}
              tickLine={false}
              axisLine={false}
              width={72}
              tickFormatter={(v: number) => formatUsd(v)}
              domain={['auto', 'auto']}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--surface-container-lowest)',
                border: '1px solid var(--outline-warm)',
                borderRadius: 8,
                fontSize: 12,
              }}
              labelStyle={{ color: 'var(--on-surface-variant)' }}
              formatter={(value, name) => [
                formatUsd(typeof value === 'number' ? value : Number(value)),
                name === 'portfolio' ? 'Portfolio' : 'SPY',
              ]}
            />
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
          </LineChart>
        </ResponsiveContainer>
      </div>
      {startDate && (
        <p className="mt-1 text-caption text-on-surface-variant">
          Since {formatDateET(`${startDate}T00:00:00Z`)} — Corollary's first run. No
          pre-Corollary history is reconstructed.
        </p>
      )}
    </div>
  )
}
