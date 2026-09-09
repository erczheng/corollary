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

afterEach(cleanup)
