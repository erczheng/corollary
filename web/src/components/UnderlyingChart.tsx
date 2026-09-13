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
import { sliceRange } from '../lib/mockData'
import { type ChartRange } from '../lib/types'
import { useUnderlyings } from '../lib/queries'
import { RequestFailed } from './RequestFailed'
import { rangeChange } from '../lib/chart'
import { formatDateOnly, formatPct, formatUsd, signClass } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

const AXIS_TICK = { fill: 'var(--on-surface-variant)', fontSize: 12 }

/** Why 400 and not 90: at a quarter of history, 3M, YTD, 1Y and All all draw
 * the same chart, and at a year 1Y and All still do. The server caps the
 * request here anyway. */
const HISTORY_DAYS = 400

/** A stock's price over a window you choose.
 *
 * Reads its own quote and series from the server, scoped to the one symbol
 * whose row is expanded — a chart is opened one at a time, and fetching 400
 * sessions for twenty-six names to draw one of them is a request budget
 * spent on nothing.
 *
 * It carries no strike and no contract. It sat in the option ticket first
 * and that was the wrong home — a chart is a thing you *browse*, and the
 * place you browse stocks is the stock table. What the ticket actually
 * needed from it was one sentence about where spot sits against the strike,
 * which is now stated there directly.
 */
export function UnderlyingChart({ symbol }: { symbol: string }) {
  const [range, setRange] = useState<ChartRange>('3M')
  const query = useUnderlyings([symbol], HISTORY_DAYS)
  const quote = query.data?.find((u) => u.symbol === symbol) ?? null

  const points = useMemo(
    () => (quote ? sliceRange(quote.history, range) : []),
    [quote, range],
  )

  // The move over the window on screen, not over the day. A range control
  // that redraws the axis but leaves a daily figure beside it is reporting
  // on a chart nobody is looking at.
  const windowChange = useMemo(
    () => (points.length > 1 ? rangeChange(points, 0, points.length - 1) : null),
    [points],
  )

  if (query.isError) {
    return <RequestFailed error={query.error} what={`the ${symbol} price history`} />
  }

  if (query.isPending) {
    return (
      <p role="status" className="text-caption text-on-surface-variant">
        Reading {symbol}'s daily series…
      </p>
    )
  }

  if (!quote) {
    return (
      <p className="text-caption text-on-surface-variant">
        The server quoted no price for {symbol}, so there is no series to draw. A symbol with no
        price is left out of the response rather than served as a row of zeroes.
      </p>
    )
  }

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
            <span className="text-data-md text-on-surface">{formatUsd(quote.price)}</span>
          </span>
          {windowChange && (
            <span className={`text-data-md ${signClass(windowChange.change)}`}>
              {formatUsd(windowChange.change, { signed: true })}{' '}
              {formatPct(windowChange.changePct, { signed: true })}
              <span className="ml-1 text-label-md text-on-surface-variant">over {range}</span>
            </span>
          )}
        </p>
      </div>

      <div className="h-48">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={points} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={formatDateOnly}
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
              labelFormatter={(d) => formatDateOnly(String(d))}
              formatter={(value) => [formatUsd(Number(value)), symbol]}
            />
            {/* Yesterday's close, so the day's move is the distance from
                this line rather than something to work out. */}
            {/* Absent on a name with no prior session — the line is then
                omitted rather than drawn at zero, which would compress the
                whole axis to make room for a price that never happened. */}
            {quote.previousClose !== null && (
            <ReferenceLine
              y={quote.previousClose}
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
            <Line type="monotone" dataKey="value" stroke="var(--primary)" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

    </div>
  )
}
