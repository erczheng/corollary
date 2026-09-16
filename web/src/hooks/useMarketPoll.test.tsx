import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, act } from '@testing-library/react'
import {
  MARKETS_BACKGROUND_POLL_MS,
  MARKETS_FOREGROUND_POLL_MS,
  useMarketPoll,
} from './useMarketPoll'

/** Decision 18's three states, and the one number the server shares.
 *
 * No `useStocks` observer is mounted anywhere here on purpose: the
 * background state *has* no observer, and counting `fetch` calls is the
 * only way to see a cadence that runs whether or not anything renders. */

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function stubFetch() {
  const fetchMock = vi.fn((_input: unknown) => Promise.resolve(jsonResponse([])))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function wrapper() {
  // `retry: false` so a count is a count: the poll's own dedupe and the
  // interval are what these tests are about, not the retryer.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return Wrapper
}

/** jsdom has no tab to hide, so `document.hidden` is redefined directly.
 * `visibilitychange` is a document event, which is what the hook listens
 * for — a window-level `blur` would be the *focus* question instead. */
function setHidden(hidden: boolean) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'))
  })
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the shared cadence constants', () => {
  /** **This test is the client half of a two-sided invariant.** The server
   * holds `MARKETS_FOREGROUND_POLL_MS = 400` in
   * `corollary/api/routes/markets.py` and derives `STOCK_SNAPSHOT_TTL` from
   * it, so that N tabs polling at this interval cost one Alpaca request per
   * interval rather than N. That claim holds only while the two numbers are
   * equal, and the server's
   * `test_the_cache_ttl_is_the_foreground_poll_interval` fails loudly if the
   * server half moves. Nothing failed if *this* half moved, which is what
   * this test closes. Move them together or not at all. */
  it('pins the foreground interval at the server cache TTL', () => {
    expect(MARKETS_FOREGROUND_POLL_MS).toBe(400)
  })

  /** 400 is a floor. 300ms is exactly 200/min with nothing left for a chain,
   * and 150/200ms exceed the bucket outright — `ratelimit.py` *waits*
   * rather than refusing, so going under would read as latency creep rather
   * than as an error. */
  it('leaves headroom under the 200/min bucket', () => {
    expect(60_000 / MARKETS_FOREGROUND_POLL_MS).toBeLessThanOrEqual(150)
    expect(60_000 / MARKETS_BACKGROUND_POLL_MS).toBeLessThanOrEqual(12)
  })
})

describe('useMarketPoll', () => {
  it('reads once immediately, then on the interval', async () => {
    const fetchMock = stubFetch()
    renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), { wrapper: wrapper() })

    // Immediately, not on the first tick: a page that showed skeletons for
    // an interval on arrival would be reporting a problem it does not have.
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(String(fetchMock.mock.calls[0][0])).toContain('/markets/stocks')

    await advance(MARKETS_FOREGROUND_POLL_MS * 3)
    expect(fetchMock).toHaveBeenCalledTimes(4)
  })

  it('stops entirely while the tab is hidden', async () => {
    const fetchMock = stubFetch()
    renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), { wrapper: wrapper() })
    await advance(MARKETS_FOREGROUND_POLL_MS)
    const before = fetchMock.mock.calls.length

    setHidden(true)
    // Not slowed to the background rate — stopped. Nothing is painted, and
    // the money-bearing numbers are on a stream that does not care.
    await advance(MARKETS_BACKGROUND_POLL_MS * 4)

    expect(fetchMock).toHaveBeenCalledTimes(before)
  })

  it('reads immediately on return, without waiting out an interval', async () => {
    const fetchMock = stubFetch()
    renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), { wrapper: wrapper() })
    setHidden(true)
    await advance(MARKETS_BACKGROUND_POLL_MS)
    const before = fetchMock.mock.calls.length

    setHidden(false)

    // Before any timer runs. A revisit that waited out an interval would
    // show a stale table for as long as it took.
    expect(fetchMock).toHaveBeenCalledTimes(before + 1)
    await advance(MARKETS_FOREGROUND_POLL_MS)
    expect(fetchMock).toHaveBeenCalledTimes(before + 2)
  })

  it('polls nothing once unmounted', async () => {
    const fetchMock = stubFetch()
    const { unmount } = renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), {
      wrapper: wrapper(),
    })
    unmount()
    const before = fetchMock.mock.calls.length

    await advance(MARKETS_BACKGROUND_POLL_MS * 2)

    expect(fetchMock).toHaveBeenCalledTimes(before)
  })

  it('mounts nothing at all for a non-positive interval', async () => {
    const fetchMock = stubFetch()
    renderHook(() => useMarketPoll(0), { wrapper: wrapper() })

    await advance(MARKETS_BACKGROUND_POLL_MS * 2)

    expect(fetchMock).not.toHaveBeenCalled()
  })

  /** The floor is **structural, not conventional**. The constant test above
   * pins the number; this pins "no subscriber may ask for less than it".
   * 200ms is 300/min against a hard 200/min bucket, and `ratelimit.py`
   * *waits* rather than refusing — so an unclamped call site would overspend
   * the vendor budget with nothing failing, surfacing as latency creep that
   * looks like it worked. */
  it('clamps a faster request up to the foreground floor', async () => {
    const fetchMock = stubFetch()
    const half = MARKETS_FOREGROUND_POLL_MS / 2
    renderHook(() => useMarketPoll(half), { wrapper: wrapper() })

    // The immediate read still happens; what is clamped is the cadence.
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await advance(MARKETS_FOREGROUND_POLL_MS)
    // One interval, not two. Unclamped this would be 3 by now.
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  /** A clamp that ran before the opt-out would turn "poll nothing" into
   * "poll at 400ms", which is the opposite of what the caller asked for. */
  it('does not clamp the opt-out into a poll', async () => {
    const fetchMock = stubFetch()
    renderHook(() => useMarketPoll(-1), { wrapper: wrapper() })

    await advance(MARKETS_FOREGROUND_POLL_MS * 4)

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

/** The three states are one hook and one constant pair, and the whole point
 * is that the app-level mount and the Markets mount never both hold a
 * timer: two intervals would double the vendor rate for as long as both
 * pages agreed to run. */
describe('foreground superseding background', () => {
  function mountBoth() {
    const Wrapper = wrapper()
    const background = renderHook(() => useMarketPoll(MARKETS_BACKGROUND_POLL_MS), {
      wrapper: Wrapper,
    })
    const foreground = renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), {
      wrapper: Wrapper,
    })
    return { background, foreground }
  }

  it('runs exactly one interval, at the faster cadence', async () => {
    const fetchMock = stubFetch()
    const { background, foreground } = mountBoth()
    const after = fetchMock.mock.calls.length

    await advance(MARKETS_BACKGROUND_POLL_MS)

    // One interval at 400ms over 5s is 12 reads. A second interval still
    // running at 5s would make it 13 — the margin is one request, which is
    // exactly why it is asserted rather than eyeballed.
    const polls = fetchMock.mock.calls.length - after
    expect(polls).toBe(Math.floor(MARKETS_BACKGROUND_POLL_MS / MARKETS_FOREGROUND_POLL_MS))

    foreground.unmount()
    background.unmount()
  })

  it('does not re-read on the cadence change itself', () => {
    const fetchMock = stubFetch()
    const Wrapper = wrapper()
    const background = renderHook(() => useMarketPoll(MARKETS_BACKGROUND_POLL_MS), {
      wrapper: Wrapper,
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    // Navigating to Markets remounts the query observer, which fetches on
    // its own; an immediate poll here as well would be the same read twice.
    const foreground = renderHook(() => useMarketPoll(MARKETS_FOREGROUND_POLL_MS), {
      wrapper: Wrapper,
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)

    foreground.unmount()
    background.unmount()
  })

  it('resumes the background cadence when Markets unmounts', async () => {
    const fetchMock = stubFetch()
    const { background, foreground } = mountBoth()

    foreground.unmount()
    const after = fetchMock.mock.calls.length

    // A foreground interval left running after the page closed would keep
    // spending the bucket on a table nobody is looking at.
    await advance(MARKETS_FOREGROUND_POLL_MS * 4)
    expect(fetchMock).toHaveBeenCalledTimes(after)

    await advance(MARKETS_BACKGROUND_POLL_MS)
    expect(fetchMock).toHaveBeenCalledTimes(after + 1)

    background.unmount()
  })
})
