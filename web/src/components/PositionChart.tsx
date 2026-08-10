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
import { type Position } from '../lib/mockData'
import { useUIStore } from '../lib/store'
import { breakevens, maxLoss, maxProfit, payoffCurve } from '../lib/orders'
import { formatDateOnly, formatPct, formatUsd, signClass } from '../lib/format'

type ChartMode = 'value' | 'payoff' | 'underlying'

const MODES: { key: ChartMode; label: string }[] = [
  { key: 'value', label: 'Value since entry' },
  { key: 'payoff', label: 'Payoff at expiry' },
  { key: 'underlying', label: 'Underlying' },
]

const AXIS_TICK = { fill: 'var(--on-surface-variant)', fontSize: 12 }

/** Two readings of the same position over one chart area.
 *
 * **Value since entry** plots what the position is worth, not what the
 * contract trades at — contract price inverts for a short, so a credit
 * spread's chart would run backwards. Cost basis is drawn as a reference
 * line, which makes unrealized P&L the gap between the series and the
 * line rather than a number you have to go and find.
 *
 * **Payoff at expiry** is computed from strikes, premium and the
 * multiplier, so it needs no market data at all and is exactly as true in
 * Phase 1 as it will be in Phase 2. */
export function PositionChart({ position }: { position: Position }) {
  const [mode, setMode] = useState<ChartMode>('value')

  const payoff = useMemo(() => payoffCurve(position), [position])
  const payoffBreakevens = useMemo(() => breakevens(payoff), [payoff])
  // From the store, not the fixture: the tick moves these, and a chart
  // reading the frozen module constant would sit still while the row above
  // it changed.
  const underlying = useUIStore((s) => s.underlyings[position.symbol]) ?? null

  return (
    <div>
      <div
        role="group"
        aria-label="Chart view"
        className="mb-3 inline-flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md"
      >
        {MODES.map((m) => (
          <button
            key={m.key}
            type="button"
            onClick={() => setMode(m.key)}
            aria-pressed={mode === m.key}
            className={
              mode === m.key
                ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
                : 'rounded-full px-3 py-1 text-on-surface-variant hover:text-on-surface'
            }
          >
            {m.label}
          </button>
        ))}
      </div>

      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          {mode === 'value' ? (
            <LineChart data={position.valueHistory} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
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
                formatter={(value) => [formatUsd(Number(value)), 'Value']}
              />
              {/* Cost basis. The gap between the line and the series is the
                  unrealized P&L, which is the whole point of the view. */}
              <ReferenceLine
                y={position.costBasis}
                stroke="var(--outline)"
                strokeDasharray="4 4"
                label={{ value: 'Cost basis', position: 'insideTopLeft', fill: 'var(--on-surface-variant)', fontSize: 12 }}
              />
              <Line type="monotone" dataKey="value" stroke="var(--primary)" strokeWidth={2} dot={false} />
            </LineChart>
          ) : mode === 'underlying' ? (
            <LineChart
              data={underlying?.history ?? []}
              margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
            >
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
                domain={['auto', 'auto']}
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
                formatter={(value) => [formatUsd(Number(value)), position.symbol]}
              />
              {/* Yesterday's close, so the day's move is the distance from
                  this line rather than something to work out. */}
              {underlying && (
                <ReferenceLine
                  y={underlying.previousClose}
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
              {/* Every strike this position holds, so you can see where the
                  underlying sits relative to the thing that decides
                  whether the trade works. */}
              {position.legs.map((leg) => (
                <ReferenceLine
                  key={leg.symbol}
                  y={leg.strike}
                  stroke="var(--accent)"
                  strokeDasharray="2 4"
                  label={{
                    value: `${leg.side === 'short' ? 'Short' : 'Long'} ${formatUsd(leg.strike)}`,
                    position: 'insideBottomRight',
                    fill: 'var(--on-surface-variant)',
                    fontSize: 12,
                  }}
                />
              ))}
              <Line type="monotone" dataKey="value" stroke="var(--primary)" strokeWidth={2} dot={false} />
            </LineChart>
          ) : (
            <LineChart data={payoff} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--outline-warm)" strokeOpacity={0.4} vertical={false} />
              <XAxis
                dataKey="underlying"
                type="number"
                domain={['dataMin', 'dataMax']}
                tick={AXIS_TICK}
                tickLine={false}
                axisLine={{ stroke: 'var(--outline-warm)' }}
                tickFormatter={(v: number) => formatUsd(v)}
                minTickGap={40}
              />
              <YAxis
                tick={AXIS_TICK}
                tickLine={false}
                axisLine={false}
                width={72}
                tickFormatter={(v: number) => formatUsd(v)}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--surface-container-lowest)',
                  border: '1px solid var(--outline-warm)',
                  borderRadius: 8,
                  color: 'var(--on-surface)',
                }}
                labelFormatter={(v) => `${position.symbol} at ${formatUsd(Number(v))}`}
                formatter={(value) => [formatUsd(Number(value), { signed: true }), 'P&L at expiry']}
              />
              {/* Zero is the line that matters here — above it the trade
                  made money, below it lost. Drawn in `outline`, not in
                  bullish or bearish, because it is the boundary between
                  them and belongs to neither. */}
              <ReferenceLine y={0} stroke="var(--outline)" />
              <ReferenceLine
                x={position.underlying}
                stroke="var(--accent)"
                strokeDasharray="4 4"
                label={{ value: 'Now', position: 'top', fill: 'var(--on-surface-variant)', fontSize: 12 }}
              />
              <Line type="linear" dataKey="pnl" stroke="var(--primary)" strokeWidth={2} dot={false} />
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>

      {mode === 'underlying' && underlying && (
        <dl className="mt-3 grid grid-cols-3 gap-4 text-label-md">
          <div>
            <dt className="text-on-surface-variant">Price</dt>
            <dd className="text-data-md text-on-surface">{formatUsd(underlying.price)}</dd>
          </div>
          <div>
            <dt className="text-on-surface-variant">Today</dt>
            {/* Sign carried textually as well as by colour, on both
                figures — the rule that applies to every P&L here. */}
            <dd className={`text-data-md ${signClass(underlying.change)}`}>
              {formatUsd(underlying.change, { signed: true })}{' '}
              <span className="text-caption">{formatPct(underlying.changePct, { signed: true })}</span>
            </dd>
          </div>
          <div>
            <dt className="text-on-surface-variant">Previous close</dt>
            <dd className="text-data-md text-on-surface">{formatUsd(underlying.previousClose)}</dd>
          </div>
          {/* The strikes as text, not only as lines on the chart. Where
              the stock sits relative to them is the question a spread
              actually raises, and a gridline you have to read off an axis
              is a poor way to answer it. */}
          <div className="col-span-3">
            <dt className="text-on-surface-variant">Strikes</dt>
            <dd className="text-data-md text-on-surface">
              {position.legs
                .map((leg) => `${leg.side === 'short' ? 'Short' : 'Long'} ${formatUsd(leg.strike)}`)
                .join(' · ')}
            </dd>
          </div>
        </dl>
      )}

      {mode === 'payoff' && (
        /* The three numbers you would otherwise read off the curve by eye.
           Max loss on a defined-risk structure is the number that decides
           whether the position is sized correctly, so it should not
           require squinting at an axis. */
        <dl className="mt-3 grid grid-cols-3 gap-4 text-label-md">
          <div>
            <dt className="text-on-surface-variant">Max profit</dt>
            <dd className={`text-data-md ${signClass(maxProfit(payoff))}`}>
              {formatUsd(maxProfit(payoff), { signed: true })}
            </dd>
          </div>
          <div>
            <dt className="text-on-surface-variant">Max loss</dt>
            <dd className={`text-data-md ${signClass(maxLoss(payoff))}`}>
              {formatUsd(maxLoss(payoff), { signed: true })}
            </dd>
          </div>
          <div>
            <dt className="text-on-surface-variant">
              {payoffBreakevens.length === 1 ? 'Breakeven' : 'Breakevens'}
            </dt>
            <dd className="text-data-md text-on-surface">
              {payoffBreakevens.length === 0
                ? '—'
                : payoffBreakevens.map((b) => formatUsd(b)).join(' · ')}
            </dd>
          </div>
        </dl>
      )}
    </div>
  )
}
