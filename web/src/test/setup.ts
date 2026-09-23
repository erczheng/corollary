import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { stopLiveSocket } from '../lib/liveSocket'
import {
  RecordingIntersectionObserver,
  resetRecordedObservers,
} from './recordingIntersectionObserver'
import { RecordingWebSocket, resetRecordedSockets } from './recordingWebSocket'

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
 * A stub rather than a guard in the component. Markets' viewport hint (step
 * 15 (b)) constructs one on mount, every browser Corollary runs in has had
 * the API for years, and a `typeof IntersectionObserver === 'undefined'`
 * branch in the page would be production code shaped around a gap in the test
 * environment — the same judgement `scrollIntoView` above is written to.
 *
 * {@link RecordingIntersectionObserver} **records constructions instead of
 * being inert**, which is finding WEB-9: an inert observer masks in one
 * direction, because a component that asks it *is this visible* takes the
 * never-visible branch forever and a test asserting **absence** passes
 * vacuously. Recording makes *nothing was observed* and *nothing was reported
 * on screen* two different assertions. It is the shape
 * {@link RecordingWebSocket} already set, so the harness has one idea.
 *
 * It still reports nothing on its own, so no test sees a hint by accident,
 * and `Markets.test.tsx` still replaces the global with a driveable one for
 * the tests that are *about* the viewport — no suite has to opt in to
 * either. */
globalThis.IntersectionObserver = RecordingIntersectionObserver

/** The live socket, replaced for the run — and **this one is a suppression,
 * not a gap.**
 *
 * The two stubs above fill holes jsdom genuinely has. jsdom implements
 * `WebSocket` for real, so leaving it alone would mean every suite that
 * renders `<App />` — twelve of them, now that `useLiveSocket` mounts at the
 * shell — opening `ws://localhost/api/ws` against nothing, failing, and
 * arming a 500ms reconnect inside the run. The alternative the spec already
 * rejected is a `typeof WebSocket === 'undefined'` branch in the hook, which
 * would be production code shaped around the test environment.
 *
 * {@link RecordingWebSocket} records constructions instead of being inert, so
 * a test can assert the mount *happened* rather than passing vacuously on its
 * absence — see the WEB-9 note there. `liveSocket.test.ts` is untouched by
 * this either way: it drives its own `SocketLike` through the `create`
 * option and never reaches the global. */
globalThis.WebSocket = RecordingWebSocket as unknown as typeof WebSocket

afterEach(cleanup)

/** Throw the app's socket away between tests.
 *
 * `useLiveSocket` deliberately registers **no** effect cleanup — the
 * socket's lifetime is the document's, not a component's, and StrictMode's
 * simulated remount must not churn the connection (the reasoning is written
 * out in the hook). That decision puts this line here rather than there:
 * unmounting in a test *is* throwing the app away, and without an explicit
 * stop the module singleton — and any reconnect timer it has armed — would
 * outlive the test that created it and be found by the next one.
 *
 * Independent of the `cleanup` above in either order: with no cleanup in the
 * hook, unmounting touches the socket not at all. */
afterEach(() => {
  stopLiveSocket()
  resetRecordedSockets()
  resetRecordedObservers()
})
