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
import { sliceRange, type ChartRange } from '../lib/mockData'
import { useUIStore } from '../lib/store'
import { rangeChange } from '../lib/chart'
import { formatDateOnly, formatPct, formatUsd, signClass } from '../lib/format'

const RANGES: ChartRange[] = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

const AXIS_TICK = { fill: 'var(--on-surface-variant)', fontSize: 12 }

/** The underlying behind the contract you are about to trade.
 *
 * An option ticket without the stock is a ticket asking you to price a
 * derivative with the derivative hidden. The strike is drawn on it, because
 * the one question this chart answers is where the underlying sits relative
 * to the number that decides whether the trade works.
 *
 * Reads the quote from the store rather than the fixture: the poll moves
 * it, and a chart on the frozen module constant would sit still under a row
 * that is changing.
 */
export function UnderlyingChart({
  symbol,
  strike,
  right,
}: {
  symbol: string
  /** The contract's strike, marked on the series. */
  strike: number
  right: 'call' | 'put'
}) {
  const [range, setRange] = useState<ChartRange>('3M')
  const quote = useUIStore((s) => s.underlyings[symbol]) ?? null

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

  if (!quote) {
    return (
      <p className="text-caption text-on-surface-variant">
        No quote for {symbol}. Phase 2 subscribes to the underlying alongside the chain.
      </p>
    )
  }

  // In the money is a fact about where the stock is, and it is the fact
  // that decides what this contract is worth at expiry.
  const itm = right === 'call' ? quote.price > strike : quote.price < strike

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
              // The strike has to stay on screen at every range, or the
              // line marking it silently leaves the plot and the chart
              // stops answering its one question.
              domain={[
                (min: number) => Math.min(min, strike) * 0.99,
                (max: number) => Math.max(max, strike) * 1.01,
              ]}
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
            {/* The strike. A stroke, not text — the accessibility floor
                that rules `accent` out as a text colour does not apply to
                a line, and the label beside it takes on-surface-variant. */}
            <ReferenceLine
              y={strike}
              stroke="var(--accent)"
              strokeDasharray="2 4"
              label={{
                value: `Strike ${formatUsd(strike)}`,
                position: 'insideBottomRight',
                fill: 'var(--on-surface-variant)',
                fontSize: 12,
              }}
            />
            <Line type="monotone" dataKey="value" stroke="var(--primary)" strokeWidth={2} dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Stated in words as well as drawn, because which side of the strike
          the stock sits on is the single fact this chart exists for, and
          reading it off a dashed line is work. */}
      <p className="mt-2 text-caption text-on-surface-variant">
        {symbol} at {formatUsd(quote.price)} is{' '}
        {itm ? 'in the money' : 'out of the money'} against the {formatUsd(strike)} {right}, by{' '}
        {formatUsd(Math.abs(quote.price - strike))}.
      </p>
    </div>
  )
}
