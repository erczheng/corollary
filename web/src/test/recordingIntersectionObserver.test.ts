import { describe, it, expect, vi } from 'vitest'
import {
  RecordingIntersectionObserver,
  recordedObservers,
} from './recordingIntersectionObserver'

/** **Finding WEB-9, pinned at the harness rather than at a page.**
 *
 * The stub this replaces was inert, and an inert observer masks in one
 * direction: a component that asks it *is this visible* takes the
 * never-visible branch forever, so a test asserting **absence** passes
 * vacuously and a real failure is silent. The record is what separates
 * *nothing was observed* from *nothing was reported on screen* — two
 * different facts that an inert stub renders identical.
 *
 * Pinned here because the record is the harness's promise to every suite,
 * not one page's behaviour: `Markets.test.tsx` swaps in a driveable observer
 * for the tests that are about the viewport, and every other suite gets this
 * one without opting in. */
describe('the recording IntersectionObserver', () => {
  it('is the observer the run sees, and records every construction in order', () => {
    const first = new IntersectionObserver(() => {})
    const second = new IntersectionObserver(() => {})

    // `setup.ts` installs it globally: a component under test constructs
    // this class without knowing it, which is what makes the record worth
    // anything.
    expect(first).toBeInstanceOf(RecordingIntersectionObserver)
    expect(recordedObservers()).toHaveLength(2)
    expect(recordedObservers()[0]).toBe(first)
    expect(recordedObservers()[1]).toBe(second)
  })

  it('records what is being watched, so an empty viewport is a real observation', () => {
    const row = document.createElement('tr')
    const other = document.createElement('tr')
    const observer = new IntersectionObserver(() => {})

    observer.observe(row)
    // A real observer ignores a repeat of the same target, and so does this.
    observer.observe(row)
    observer.observe(other)
    // In DOM order, which is the order the viewport hint has to be in — so a
    // test can assert the rows were watched at all before asserting which of
    // them the page reported.
    expect(recordedObservers()[0].watching).toEqual([row, other])

    observer.unobserve(row)
    expect(recordedObservers()[0].watching).toEqual([other])
  })

  it('forgets its targets on disconnect, and says that it was disconnected', () => {
    const observer = new IntersectionObserver(() => {})
    observer.observe(document.createElement('tr'))

    // Markets disconnects and rebuilds per set of rendered rows, so a page
    // turn appends a second observer rather than mutating the first — and a
    // leaked observer is visible as one that never disconnected.
    observer.disconnect()
    expect(recordedObservers()[0].watching).toEqual([])
    expect(recordedObservers()[0].disconnected).toBe(true)
  })

  it('reports nothing on its own, because there is no layout to report', () => {
    const callback = vi.fn()
    const observer = new IntersectionObserver(callback)
    observer.observe(document.createElement('tr'))

    // WEB-9 removes the mask; it does not invent an answer jsdom cannot
    // have. An entry fabricated here would be fiction, and a test that
    // needs to *say* what is on screen replaces the global with a driveable
    // observer instead.
    expect(callback).not.toHaveBeenCalled()
    expect(observer.takeRecords()).toEqual([])
  })
})
