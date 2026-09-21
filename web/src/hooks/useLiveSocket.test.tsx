import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { StrictMode } from 'react'
import { act, render } from '@testing-library/react'
import App from '../App'
import { liveSocket, stopLiveSocket, RECONNECT_BASE_MS } from '../lib/liveSocket'
import { queryClient } from '../lib/queryClient'
import { useUIStore } from '../lib/store'
import { recordedSockets } from '../test/recordingWebSocket'

/** Step 15 (c): the mount. (a) built the client and (b) built the viewport
 * hint; this is the hook that finally calls `startLiveSocket`, so what is
 * under test is the *lifetime* — one socket, one mount, and an engine that
 * is untouched by either.
 *
 * The socket itself is `src/test/recordingWebSocket.ts`, installed for the
 * whole run in `src/test/setup.ts`: it records constructions and does
 * nothing on its own, so "the app connected" is an assertion here rather
 * than an absence that passes vacuously. */

const initialState = useUIStore.getState()

beforeEach(() => {
  // BrowserRouter reads window.location.
  window.history.pushState({}, '', '/')
  // Rule 5: every start is Paper.
  useUIStore.setState({ ...initialState, accountMode: 'paper' }, true)
  queryClient.clear()
  // No network in a unit test. A rejection is the honest stand-in — it is
  // what the page already handles when the engine is not running, and it
  // keeps the background market poll from reaching for a real socket on
  // port 3000.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.reject(new Error('no network in tests'))),
  )
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('useLiveSocket', () => {
  it('opens one connection to /api/ws when the app mounts', () => {
    expect(liveSocket()).toBeNull()

    render(<App />)

    expect(recordedSockets()).toHaveLength(1)
    expect(liveSocket()).not.toBeNull()
  })

  /** Rule 6, on the one string this step puts on the wire. The host comes
   * from the page and is deliberately not named anywhere — the terminal
   * binds to loopback and must work with no internet — so this asserts the
   * *shape*: a `ws:` scheme against whatever host the page is on, the
   * `/api/ws` path, and nothing else. No token, no query string. */
  it('connects with no credential and no query string', () => {
    render(<App />)

    const { url } = recordedSockets()[0]
    expect(url).toMatch(/^ws:\/\/[^/]+\/api\/ws$/)
    expect(url).not.toContain('?')
  })

  /** One browser, one socket. Twelve suites render `<App />` and the
   * singleton is what makes that safe: a second mount finds the connection
   * already running and `start()` is a no-op against it. */
  it('starts once across a second mount', () => {
    render(<App />)
    const first = liveSocket()

    render(<App />)

    expect(recordedSockets()).toHaveLength(1)
    expect(liveSocket()).toBe(first)
  })

  /** StrictMode runs the effect, its cleanup, and the effect again. With no
   * cleanup registered that is start → nothing → start, and the second
   * start is idempotent — so development opens exactly one connection
   * rather than cycling one on every boot. */
  it('opens one connection under StrictMode double-invoke', () => {
    render(
      <StrictMode>
        <App />
      </StrictMode>,
    )

    expect(recordedSockets()).toHaveLength(1)
    expect(liveSocket()).not.toBeNull()
  })

  /** The cleanup decision, asserted rather than implied. Unmounting
   * `AppShell` in the running terminal means the document is going away,
   * where the browser closes the socket itself; the hook therefore returns
   * no cleanup, and the socket outlives the React tree. */
  it('does not close the socket when the app unmounts', () => {
    const view = render(<App />)
    const socket = recordedSockets()[0]

    view.unmount()

    expect(socket.closeCount).toBe(0)
    expect(liveSocket()).not.toBeNull()
  })

  /** The other half of that decision: because the hook does not stop the
   * socket, something has to, and in a test that is the suite-wide
   * `afterEach` in `src/test/setup.ts` calling this same function. It
   * closes the live connection and forgets the singleton. */
  it('is ended by stopLiveSocket, which closes the connection', () => {
    render(<App />)
    const socket = recordedSockets()[0]
    act(() => socket.driveOpen())

    stopLiveSocket()

    expect(socket.closeCount).toBe(1)
    expect(liveSocket()).toBeNull()
  })

  /** And the part that matters most for a test run: the stop disarms a
   * reconnect the drop already armed, so no timer — and no second
   * connection — crosses into the next test. A dropped socket needs no
   * closing, which is why this asserts the absent reconnect rather than a
   * `close()` the client already took as read. */
  it('leaves no reconnect timer behind', () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render(<App />)
    const socket = recordedSockets()[0]

    act(() => socket.driveClose())
    stopLiveSocket()
    // Well past the 500ms reconnect, and still short of the 5s background
    // market poll, which is a different timer and not what this is about.
    act(() => {
      vi.advanceTimersByTime(4_000)
    })

    expect(recordedSockets()).toHaveLength(1)
    expect(liveSocket()).toBeNull()
  })

  /** Rule 9, against the store rather than against a comment.
   *
   * A halt stops new entries; it does not stop marking what is held (rule
   * 7), so the socket connects while halted — and stays halted. The
   * reconnect is the dangerous half: a feed that came back and quietly
   * refetched engine state would be a reconnect reading as a resume, which
   * is the one thing the dead-man's switch exists to prevent. Engine state
   * and notifications stay on their poll, so no query is invalidated on
   * open either. */
  it('never resumes the engine, on mount or on reconnect', () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    useUIStore.setState({ isHalted: true })
    const engineState = () => {
      const s = useUIStore.getState()
      return {
        isHalted: s.isHalted,
        executionMode: s.executionMode,
        activeStrategyId: s.activeStrategyId,
        accountMode: s.accountMode,
      }
    }
    const before = engineState()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')

    render(<App />)

    // Halted, and it still connects: pricing what is held is not an entry.
    expect(recordedSockets()).toHaveLength(1)
    expect(engineState()).toEqual(before)
    const invalidationsAtMount = invalidate.mock.calls.length

    const first = recordedSockets()[0]
    act(() => first.driveOpen())
    expect(engineState()).toEqual(before)
    // The client volunteers nothing on open. The one thing a reconnect
    // replays is the viewport hint, and none was ever sent here.
    expect(first.sent).toEqual([])

    act(() => first.driveClose())
    act(() => {
      vi.advanceTimersByTime(RECONNECT_BASE_MS)
    })
    expect(recordedSockets()).toHaveLength(2)

    const second = recordedSockets()[1]
    act(() => second.driveOpen())

    expect(engineState()).toEqual(before)
    expect(useUIStore.getState().isHalted).toBe(true)
    expect(second.sent).toEqual([])
    expect(invalidate.mock.calls.length).toBe(invalidationsAtMount)

    invalidate.mockRestore()
  })
})
