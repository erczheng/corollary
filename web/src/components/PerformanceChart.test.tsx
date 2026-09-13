import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PerformanceChart } from './PerformanceChart'
import type { PricePoint } from '../lib/types'

const history: PricePoint[] = [
  { date: '2025-08-08', value: 25_000 },
  { date: '2025-08-11', value: 25_400 },
  { date: '2025-08-12', value: 24_900 },
  { date: '2025-08-13', value: 26_100 },
]

describe('PerformanceChart', () => {
  it('dates the series start from the calendar date, not a day early', () => {
    render(<PerformanceChart history={history} />)

    // The series starts 2025-08-08. Rendering that date-only value in ET
    // used to print Aug 7.
    expect(screen.getByText(/from Aug 8, 2025/)).toBeInTheDocument()
    expect(screen.queryByText(/from Aug 7, 2025/)).not.toBeInTheDocument()
  })

  it('offers the full set of ranges and defaults to 3M', () => {
    render(<PerformanceChart history={history} />)

    for (const r of ['1D', '1W', '1M', '3M', 'YTD', '1Y', 'All']) {
      expect(screen.getByRole('button', { name: r })).toBeInTheDocument()
    }
    expect(screen.getByRole('button', { name: '3M' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('tells you the drag gesture exists, since nothing else advertises it', () => {
    render(<PerformanceChart history={history} />)

    expect(screen.getByText(/Drag across the chart to measure a period/)).toBeInTheDocument()
  })

  it('shows no measurement readout until a drag actually happens', () => {
    render(<PerformanceChart history={history} />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  /** Phase 2 — the series is Alpaca's own equity curve, so the footer must
   * not claim the first point as Corollary's start. Spec decision 6 marks
   * t₀ precisely so the chart cannot take credit for manual trading. */
  describe('whose history it is', () => {
    it('names the stretch that predates Corollary when t₀ falls inside the window', () => {
      render(<PerformanceChart history={history} t0="2025-08-12T14:00:00Z" />)

      expect(screen.getByText(/predates Corollary’s first run/)).toBeInTheDocument()
      expect(screen.queryByText(/Entirely since/)).not.toBeInTheDocument()
    })

    it('says the whole window is Corollary’s when t₀ is older than the first point', () => {
      render(<PerformanceChart history={history} t0="2025-01-04T14:00:00Z" />)

      expect(screen.getByText(/Entirely since Corollary’s first run/)).toBeInTheDocument()
    })

    it('admits t₀ is unrecorded rather than claiming the curve either way', () => {
      render(<PerformanceChart history={history} t0={null} />)

      expect(screen.getByText(/first run is not recorded/)).toBeInTheDocument()
      expect(screen.queryByText(/predates/)).not.toBeInTheDocument()
    })

    it('no longer claims the first point as Corollary’s first run', () => {
      render(<PerformanceChart history={history} />)

      // The old wording — true of a fixture generated at Corollary's start,
      // false of a broker curve that predates the engine entirely.
      expect(screen.queryByText(/Since Aug 8, 2025 — Corollary/)).not.toBeInTheDocument()
    })
  })

  it('offers no SPY overlay, because the only SPY series here was a fixture', () => {
    render(<PerformanceChart history={history} />)

    // `BENCHMARK_HISTORY` is a seeded random walk based at $25,000. Drawn
    // against a real equity curve it is an invented line at the wrong
    // scale — PRD §8.5.
    expect(screen.queryByRole('checkbox', { name: /SPY/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/Compare to SPY/)).not.toBeInTheDocument()
  })
})
