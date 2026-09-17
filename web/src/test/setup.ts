import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'

/** jsdom implements no layout, so it ships no `scrollIntoView` at all — the
 * property is simply absent and calling it throws.
 *
 * Stubbed here rather than guarded at the call site: the Research chat scrolling
 * its transcript into view is correct behaviour, and adding `?.()` in the
 * component would be shaping production code around a gap in the test
 * environment. Anything that genuinely depends on scroll position has to be
 * checked in a real browser regardless. */
Element.prototype.scrollIntoView = () => {}

/** jsdom implements no layout, so it ships no `IntersectionObserver` either
 * — and for the same reason it never could: "is this element on screen" has
 * no answer where nothing is laid out.
 *
 * An inert stub rather than a guard in the component. Markets' viewport hint
 * (step 15 (b)) constructs one on mount, every browser Corollary runs in has
 * had the API for years, and a `typeof IntersectionObserver === 'undefined'`
 * branch in the page would be production code shaped around a gap in the test
 * environment — the same judgement `scrollIntoView` above is written to.
 *
 * It observes and reports nothing, so no test sees a hint by accident.
 * `Markets.test.tsx` replaces it with a driveable one for the tests that are
 * *about* the viewport. */
class InertIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null
  readonly rootMargin: string = '0px'
  // Recent lib.dom, and required on the interface.
  readonly scrollMargin: string = '0px'
  readonly thresholds: readonly number[] = [0]
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

globalThis.IntersectionObserver = InertIntersectionObserver

afterEach(cleanup)
