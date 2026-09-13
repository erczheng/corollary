import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PerformanceChart } from './PerformanceChart'
import { ApiError, type ChartSeries } from '../lib/api'

const daily: ChartSeries = {
  resolution: 'daily',
  points: [
    { key: '2025-08-08', value: 25_000 },
    { key: '2025-08-11', value: 25_400 },
    { key: '2025-08-12', value: 24_900 },
    { key: '2025-08-13', value: 26_100 },
  ],
}

/** One session at five-minute bars — what `1D` asks for now. The keys are
 * instants, not dates, which is the half of this chart that renders in ET. */
const intraday: ChartSeries = {
  resolution: 'intraday',
  points: [
    { key: '2025-08-13T13:30:00Z', value: 25_900 },
    { key: '2025-08-13T13:35:00Z', value: 25_940 },
    { key: '2025-08-13T19:55:00Z', value: 26_100 },
  ],
}

const RANGES = ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']

function renderChart(props: Partial<React.ComponentProps<typeof PerformanceChart>> = {}) {
  const onRangeChange = vi.fn()
  render(
    <PerformanceChart
      series={daily}
      range="3M"
      onRangeChange={onRangeChange}
      accountLabel="Paper"
      {...props}
    />,
  )
  return { onRangeChange }
}

describe('PerformanceChart', () => {
  it('dates the series start from the calendar date, not a day early', () => {
    renderChart()

    // The series starts 2025-08-08. Rendering that date-only value in ET
    // used to print Aug 7.
    expect(screen.getByText(/from Aug 8, 2025/)).toBeInTheDocument()
    expect(screen.queryByText(/from Aug 7, 2025/)).not.toBeInTheDocument()
  })

  it('offers the full set of ranges and marks the selected one', () => {
    renderChart({ range: '1D', series: intraday })

    for (const r of RANGES) {
      expect(screen.getByRole('button', { name: r })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: '1D' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '3M' })).toHaveAttribute('aria-pressed', 'false')
  })

  /** The defect this replaces: the control sliced one fixed daily series, so
   * `1D` drew a single point. The range is now the page's, because the page
   * makes the request. */
  it('reports a range change rather than slicing what it already has', () => {
    const { onRangeChange } = renderChart()

    fireEvent.click(screen.getByRole('button', { name: '1W' }))

    expect(onRangeChange).toHaveBeenCalledWith('1W')
  })

  it('renders an intraday window in ET, where the key is an instant', () => {
    renderChart({ range: '1D', series: intraday })

    // 13:30Z is 9:30 ET — the opening bar. Formatted UTC it would read
    // 1:30 PM, and treated as a date it would not render at all.
    expect(screen.getByText(/from Aug 13, 9:30 AM ET/)).toBeInTheDocument()
  })

  it('tells you the drag gesture exists, since nothing else advertises it', () => {
    renderChart()

    expect(screen.getByText(/Drag across the chart to measure a period/)).toBeInTheDocument()
  })

  it('shows no measurement readout until a drag actually happens', () => {
    renderChart()

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  /* ---------------------------------------------------------------------
   * The states around the chart. Each one keeps the range control mounted:
   * a window the server refuses, or an account with no session inside it,
   * must still leave a way back to a range that works.
   * ------------------------------------------------------------------ */

  describe('while there is nothing to draw', () => {
    it('keeps the ranges reachable through a failed request', () => {
      renderChart({
        isError: true,
        error: new ApiError({
          status: 422,
          code: 'invalid_series_window',
          message: 'Ask for 1W at 5Min instead, or shorten the period.',
          url: '/api/account/history',
        }),
        series: { resolution: null, points: [] },
      })

      // The server's own wording, not ours: it names the finest timeframe
      // that would have fit.
      expect(screen.getByRole('alert')).toHaveTextContent('Ask for 1W at 5Min instead')
      for (const r of RANGES) {
        expect(screen.getByRole('button', { name: r })).toBeInTheDocument()
      }
    })

    it('keeps them reachable while the first window is still loading', () => {
      renderChart({ isPending: true, series: { resolution: null, points: [] } })

      expect(screen.getByText('Loading the equity curve')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: '1D' })).toBeInTheDocument()
      // Nothing is drawn, so nothing is attributed either.
      expect(screen.queryByText(/Drag across the chart/)).not.toBeInTheDocument()
    })

    it('names the window when the account has no session inside it', () => {
      renderChart({ range: '1D', series: { resolution: null, points: [] } })

      expect(screen.getByText(/No equity history for Paper in this window/)).toBeInTheDocument()
    })

    it('says a single close has no shape yet rather than drawing a flat line', () => {
      renderChart({ series: { resolution: 'daily', points: [daily.points[0]] } })

      expect(screen.getByText(/needs two closes/)).toBeInTheDocument()
    })

    it('says the same about a single intraday bar, in the unit it arrived in', () => {
      renderChart({ range: '1D', series: { resolution: 'intraday', points: [intraday.points[0]] } })

      expect(screen.getByText(/needs two points/)).toBeInTheDocument()
    })
  })

  /** A range switch keeps the previous window drawn rather than blanking —
   * an empty plot between two ranges reads as "there is no data" — but
   * anything that describes the window waits for the window it describes. */
  it('says which range it is reading while the previous one is still on screen', () => {
    renderChart({ range: '1W', stale: true })

    expect(screen.getByRole('status')).toHaveTextContent('Reading 1W…')
  })

  /** Phase 2 — the series is Alpaca's own equity curve, so the footer must
   * not claim the first point as Corollary's start. Spec decision 6 marks
   * t₀ precisely so the chart cannot take credit for manual trading. */
  describe('whose history it is', () => {
    it('names the stretch that predates Corollary when t₀ falls inside the window', () => {
      renderChart({ t0: '2025-08-12T14:00:00Z' })

      expect(screen.getByText(/predates Corollary’s first run/)).toBeInTheDocument()
      expect(screen.queryByText(/Entirely since/)).not.toBeInTheDocument()
    })

    it('says the whole window is Corollary’s when t₀ is older than the first point', () => {
      renderChart({ t0: '2025-01-04T14:00:00Z' })

      expect(screen.getByText(/Entirely since Corollary’s first run/)).toBeInTheDocument()
    })

    it('compares dates even when the series is keyed by instants', () => {
      renderChart({ range: '1D', series: intraday, t0: '2025-01-04T14:00:00Z' })

      expect(screen.getByText(/Entirely since Corollary’s first run/)).toBeInTheDocument()
    })

    it('admits t₀ is unrecorded rather than claiming the curve either way', () => {
      renderChart({ t0: null })

      expect(screen.getByText(/first run is not recorded/)).toBeInTheDocument()
      expect(screen.queryByText(/predates/)).not.toBeInTheDocument()
    })

    it('no longer claims the first point as Corollary’s first run', () => {
      renderChart()

      // The old wording — true of a fixture generated at Corollary's start,
      // false of a broker curve that predates the engine entirely.
      expect(screen.queryByText(/Since Aug 8, 2025 — Corollary/)).not.toBeInTheDocument()
    })
  })

  it('offers no SPY overlay, because the only SPY series here was a fixture', () => {
    renderChart()

    // `BENCHMARK_HISTORY` is a seeded random walk based at $25,000. Drawn
    // against a real equity curve it is an invented line at the wrong
    // scale — PRD §8.5.
    expect(screen.queryByRole('checkbox', { name: /SPY/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/Compare to SPY/)).not.toBeInTheDocument()
  })
})
