import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FixtureMarker } from './FixtureMarker'

const DETAIL = 'Every figure on this panel is sample data. The pipeline behind it is a later phase.'

/** The colour is the whole argument of this component and it lived only in a
 * comment, which is the part a future diff deletes. `neutral` says "this is a
 * stated condition of the build". `error` would say the pipeline *failed* —
 * across three pages, to an operator who would reasonably go looking for the
 * fault. `bearish` would collapse the loss/fault split the design docs hold
 * apart. Both substitutions are one word away and neither would fail a page
 * test, which assert presence and scoping. */
describe('the variant', () => {
  it('renders in neutral, never in error or bearish', () => {
    render(<FixtureMarker detail={DETAIL} />)
    const marker = screen.getByText('Sample data — Phase 1')

    expect(marker.className).toContain('bg-neutral-container')
    expect(marker.className).toContain('text-on-neutral-container')
    expect(marker.className).not.toContain('error')
    expect(marker.className).not.toContain('bearish')
  })

  /** `caution` is argued against for a different reason — it is not a
   * misreading, it is amber inflation beside the demoted-source banner — and
   * `accent` is capped at two ranked roles per screen. */
  it('spends neither caution nor accent on a build-stage label', () => {
    render(<FixtureMarker detail={DETAIL} />)
    const marker = screen.getByText('Sample data — Phase 1')

    expect(marker.className).not.toContain('caution')
    expect(marker.className).not.toContain('accent')
  })

  it('reads as status rather than as a control', () => {
    render(<FixtureMarker detail={DETAIL} />)
    const marker = screen.getByText('Sample data — Phase 1')

    expect(marker.className).toContain('rounded-full')
    expect(marker.closest('button')).toBeNull()
  })
})

/** `title` is mouse-only here: the chip is a non-focusable span, so there is
 * no keyboard path to the tooltip, and `title` on a role-less generic is
 * announced inconsistently. The visible label already says *whether* the
 * surface is a fixture; what the second copy carries is *what* is invented. */
describe('the detail', () => {
  it('is reachable without a pointer, in the same words as the tooltip', () => {
    render(<FixtureMarker detail={DETAIL} />)

    expect(screen.getByText('Sample data — Phase 1').getAttribute('title')).toBe(DETAIL)
    expect(screen.getByText(DETAIL)).toHaveClass('sr-only')
  })

  it('keeps the tooltip but drops the second copy where the page already says it', () => {
    render(<FixtureMarker detail={DETAIL} detailRestated />)

    expect(screen.getByText('Sample data — Phase 1').getAttribute('title')).toBe(DETAIL)
    expect(screen.queryByText(DETAIL)).not.toBeInTheDocument()
  })
})
