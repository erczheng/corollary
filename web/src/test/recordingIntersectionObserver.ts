/** The `IntersectionObserver` the whole Vitest run sees — **a gap, not a
 * suppression.**
 *
 * That distinction is why this file reads like {@link
 * ../test/recordingWebSocket!RecordingWebSocket} and means the opposite thing
 * by it. jsdom implements `WebSocket` for real, so the stub beside this one
 * *suppresses* a connection that would otherwise happen. jsdom implements no
 * layout, so it ships no `IntersectionObserver` at all and never could —
 * "is this element on screen" has no answer where nothing is laid out. This
 * one fills a hole, and the alternative the spec already rejected is a
 * `typeof IntersectionObserver === 'undefined'` branch in the page, which
 * would be production code shaped around the test environment.
 *
 * **Recording rather than inert — finding WEB-9.** The stub this replaces was
 * inert, and "masks in one direction: a future component that uses it to
 * decide *is this visible* takes the never-visible branch forever, so a test
 * asserting **absence** passes vacuously. Without the stub such a failure is
 * loud." Recording turns that hazard into an assertion a test can make: the
 * constructions are on the record, so *nothing was observed* and *nothing was
 * reported on screen* stop being the same observation. It is the same shape
 * `recordingWebSocket.ts` already took, so the harness has one idea rather
 * than two.
 *
 * **Nothing intersects on its own, and nothing here can make it.** This
 * reports no entries, ever: there is no layout to derive one from, so an
 * entry invented here would be fiction. A test that needs to *say* which rows
 * are on screen replaces the global with a driveable observer of its own —
 * `Markets.test.tsx` does exactly that for the tests that are about the
 * viewport, and needs no opt-in from any other suite to do it.
 *
 * Test-only. Two importers, `setup.ts` and this module's own test, and no
 * path to it from production code. */
export class RecordingIntersectionObserver implements IntersectionObserver {
  readonly root: Element | Document | null = null
  readonly rootMargin: string = '0px'
  // Recent lib.dom, and required on the interface.
  readonly scrollMargin: string = '0px'
  readonly thresholds: readonly number[] = [0]

  /** Held so a test can assert *what* was set up to be watched, and never
   * called from here — see the class docstring. */
  readonly callback: IntersectionObserverCallback
  /** The targets currently watched, in the order they were observed. A page
   * turn disconnects and rebuilds, so this is one page's rows and not a
   * session's. */
  readonly watching: Element[] = []
  /** Whether this observer was disconnected — the cleanup half of the same
   * question, and the one a leak would show up in. */
  disconnected = false

  constructor(callback: IntersectionObserverCallback) {
    this.callback = callback
    constructions.push(this)
  }

  observe(target: Element): void {
    // A real observer ignores a repeat `observe` of the same target.
    if (!this.watching.includes(target)) this.watching.push(target)
  }

  unobserve(target: Element): void {
    const at = this.watching.indexOf(target)
    if (at !== -1) this.watching.splice(at, 1)
  }

  disconnect(): void {
    this.watching.length = 0
    this.disconnected = true
  }

  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

const constructions: RecordingIntersectionObserver[] = []

/** Every observer constructed since the last reset, oldest first.
 *
 * Markets rebuilds its observer per set of rendered rows, so a page turn
 * appends: index 0 watched the first page, the last entry is the current
 * one. */
export function recordedObservers(): readonly RecordingIntersectionObserver[] {
  return constructions
}

/** Forget the record. Called from the suite-wide `afterEach` so a count in
 * one test is never a count of another test's renders. */
export function resetRecordedObservers(): void {
  constructions.length = 0
}
