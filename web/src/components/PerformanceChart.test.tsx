import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PerformanceChart } from './PerformanceChart'
import type { PricePoint } from '../lib/mockData'

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
    expect(screen.getByText(/Since Aug 8, 2025/)).toBeInTheDocument()
    expect(screen.queryByText(/Since Aug 7, 2025/)).not.toBeInTheDocument()
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
})
