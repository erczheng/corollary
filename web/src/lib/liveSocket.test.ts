import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  LiveSocket,
  RECONNECT_BASE_MS,
  RECONNECT_MAX_MS,
  RECONNECT_STABLE_MS,
  SOCKET_OPEN,
  liveSocketUrl,
  midOf,
  parseServerFrame,
  storeHandlers,
  type SocketLike,
} from './liveSocket'
// The poll's own producer, so the held entry in the WEB-12 block below is
// built the way the app builds one rather than hand-shaped.
import { liveFromStockQuote } from './quotes'
import { useUIStore } from './store'
import type { WsQuote } from './types'
// The module's own text, for the absence check at the bottom of this file.
// A `?raw` import rather than `node:fs`: there is no `@types/node` here, and
// Vite resolves this the same way in the test run as in a build.
import liveSocketSource from './liveSocket.ts?raw'

const initialState = useUIStore.getState()

beforeEach(() => {
  useUIStore.setState(initialState, true)
})

/* -------------------------------------------------------------------------
 * A socket that opens nothing
 * ---------------------------------------------------------------------- */

const CONNECTING = 0
const CLOSED = 3

class FakeSocket implements SocketLike {
  readyState = CONNECTING
  readonly sent: string[] = []
  closedByClient = false
  onopen: ((event: unknown) => void) | null = null
  onclose: ((event: unknown) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null

  readonly url: string

  constructor(url: string) {
    this.url = url
  }

  send(data: string): void {
    if (this.readyState !== SOCKET_OPEN) throw new Error('socket is not open')
    this.sent.push(data)
  }

  close(): void {
    this.closedByClient = true
    this.readyState = CLOSED
  }

  /* The server's side of the wire, driven by the test. */
  accept(): void {
    this.readyState = SOCKET_OPEN
    this.onopen?.({})
  }

  deliver(data: unknown): void {
    this.onmessage?.({ data })
  }

  drop(): void {
    this.readyState = CLOSED
    this.onclose?.({})
  }

  get messages(): unknown[] {
    return this.sent.map((text) => JSON.parse(text) as unknown)
  }
}

function harness(now = () => 0) {
  const sockets: FakeSocket[] = []
  const create = (url: string): SocketLike => {
    const socket = new FakeSocket(url)
    sockets.push(socket)
    return socket
  }
  const latest = (): FakeSocket => {
    const socket = sockets[sockets.length - 1]
    if (!socket) throw new Error('no socket was created')
    return socket
  }
  return { sockets, create, latest, now }
}

const QUOTE: WsQuote = {
  symbol: 'AAPL',
  bid: 189.98,
  ask: 190.02,
  bidSize: 4,
  askSize: 7,
  at: '2026-09-17T14:30:00Z',
}

function quoteFrame(quote: Partial<WsQuote> = {}): string {
  return JSON.stringify({ type: 'quote', quote: { ...QUOTE, ...quote } })
}

/* -------------------------------------------------------------------------
 * The URL
 * ---------------------------------------------------------------------- */

describe('liveSocketUrl', () => {
  it('follows the page host and never names one of its own', () => {
    expect(liveSocketUrl('/api', 'http://127.0.0.1:5173/markets')).toBe(
      'ws://127.0.0.1:5173/api/ws',
    )
  })

  it('takes wss on an https page', () => {
    expect(liveSocketUrl('/api', 'https://desk.local/dashboard')).toBe('wss://desk.local/api/ws')
  })

  it('resolves an absolute VITE_API_BASE and swaps only the scheme', () => {
    expect(liveSocketUrl('http://127.0.0.1:8000/api', 'http://127.0.0.1:5173/')).toBe(
      'ws://127.0.0.1:8000/api/ws',
    )
  })

  it('leaves a base that is already a websocket scheme alone', () => {
    expect(liveSocketUrl('ws://127.0.0.1:8000/api', 'http://127.0.0.1:5173/')).toBe(
      'ws://127.0.0.1:8000/api/ws',
    )
  })
})

/* -------------------------------------------------------------------------
 * The midpoint — the three branches, and there is no `mid` on the wire
 * ---------------------------------------------------------------------- */

describe('midOf', () => {
  it('averages a two-sided quote', () => {
    expect(midOf(189.98, 190.02)).toBe(190)
  })

  it('prices a locked book, which is ordinary rather than crossed', () => {
    expect(midOf(190, 190)).toBe(190)
  })

  it('has no midpoint with no bid', () => {
    expect(midOf(null, 190.02)).toBeNull()
  })

  it('has no midpoint with no ask', () => {
    expect(midOf(189.98, null)).toBeNull()
  })

  it('has no midpoint on a crossed quote, which is a data error not a market', () => {
    expect(midOf(190.5, 190.02)).toBeNull()
  })

  it('has no midpoint for a non-finite side, which `>` would let through', () => {
    expect(midOf(Number.NaN, 190.02)).toBeNull()
    expect(midOf(189.98, Number.POSITIVE_INFINITY)).toBeNull()
  })
})

/* -------------------------------------------------------------------------
 * The three-kind frame contract
 * ---------------------------------------------------------------------- */

describe('parseServerFrame', () => {
  it('reads a quote frame', () => {
    const frame = parseServerFrame(quoteFrame())
    expect(frame).toEqual({ kind: 'quote', quote: QUOTE })
  })

  it('reads a trade_update frame from its own key', () => {
    const update = {
      event: 'fill',
      at: '2026-09-17T14:31:00Z',
      orderId: 'ord-1',
      symbol: 'AAPL251219C00190000',
      status: 'filled',
      action: 'BTO',
      quantity: 2,
      filledQuantity: 2,
      fillPrice: 3.4,
      fillQuantity: 2,
      filledAvgPrice: 3.4,
      positionQuantity: 2,
    }
    expect(parseServerFrame(JSON.stringify({ type: 'trade_update', update }))).toEqual({
      kind: 'trade_update',
      update,
    })
  })

  it('reads an error frame through the one error parser', () => {
    const text = JSON.stringify({
      type: 'error',
      error: { code: 'subscription_refused', message: '65 symbols is more than a viewport may name.' },
    })
    expect(parseServerFrame(text)).toEqual({
      kind: 'error',
      error: {
        code: 'subscription_refused',
        message: '65 symbols is more than a viewport may name.',
      },
    })
  })

  it('reports an unknown `type` rather than guessing at its fields', () => {
    // A fourth kind is a decision, not an addition. If one ever arrives, a
    // client one version behind says so and drops it — it does not sniff
    // the payload and it does not throw.
    const text = JSON.stringify({ type: 'engine_state', engineState: { halted: true } })
    expect(parseServerFrame(text)).toEqual({ kind: 'unknown', type: 'engine_state' })
  })

  it('dispatches on the discriminant, never on which fields are present', () => {
    // A quote payload under an `error` tag is unreadable, not a quote. The
    // whole point of the discriminator is that the payload is never
    // consulted to decide the kind.
    const text = JSON.stringify({ type: 'error', quote: QUOTE })
    expect(parseServerFrame(text).kind).toBe('unreadable')
  })

  it('treats a payload missing under its own tag as unreadable', () => {
    expect(parseServerFrame(JSON.stringify({ type: 'quote' })).kind).toBe('unreadable')
  })

  it('survives text that is not JSON, and a frame that is not an object', () => {
    expect(parseServerFrame('<html>502 Bad Gateway</html>').kind).toBe('unreadable')
    expect(parseServerFrame(JSON.stringify([1, 2, 3])).kind).toBe('unreadable')
    expect(parseServerFrame(new ArrayBuffer(8)).kind).toBe('unreadable')
  })

  it('reports a non-string `type` as unknown with no tag to name', () => {
    expect(parseServerFrame(JSON.stringify({ type: 7 }))).toEqual({ kind: 'unknown', type: null })
  })
})

/* -------------------------------------------------------------------------
 * Routing
 * ---------------------------------------------------------------------- */

describe('LiveSocket routing', () => {
  it('routes each of the three kinds to its own handler', () => {
    const rig = harness()
    const onQuote = vi.fn()
    const onTradeUpdate = vi.fn()
    const onError = vi.fn()
    const onUnusable = vi.fn()
    const socket = new LiveSocket({
      url: 'ws://127.0.0.1/api/ws',
      create: rig.create,
      onQuote,
      onTradeUpdate,
      onError,
      onUnusable,
    })
    socket.start()
    rig.latest().accept()

    rig.latest().deliver(quoteFrame())
    rig.latest().deliver(
      JSON.stringify({ type: 'trade_update', update: { event: 'fill', orderId: 'ord-1' } }),
    )
    rig.latest().deliver(
      JSON.stringify({ type: 'error', error: { code: 'invalid_request', message: 'no' } }),
    )
    rig.latest().deliver(JSON.stringify({ type: 'something_new', payload: {} }))

    expect(onQuote).toHaveBeenCalledTimes(1)
    expect(onQuote.mock.calls[0]?.[0]).toEqual(QUOTE)
    expect(onTradeUpdate).toHaveBeenCalledTimes(1)
    expect(onError).toHaveBeenCalledWith({ code: 'invalid_request', message: 'no' })
    expect(onUnusable).toHaveBeenCalledWith({ kind: 'unknown', type: 'something_new' })
    socket.stop()
  })

  it('stays open on a refused subscription — a refusal is not a dead socket', () => {
    const rig = harness()
    const onError = vi.fn()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create, onError })
    socket.start()
    rig.latest().accept()

    socket.sendMarketsVisible(['AAPL'])
    rig.latest().deliver(
      JSON.stringify({
        type: 'error',
        error: {
          code: 'subscription_refused',
          message: '1 of 1 entries are option contracts. The whole message is refused.',
        },
      }),
    )

    expect(onError).toHaveBeenCalledTimes(1)
    expect(socket.status).toBe('open')
    expect(socket.connected).toBe(true)
    expect(rig.latest().closedByClient).toBe(false)
    expect(rig.sockets).toHaveLength(1)

    // And the feed keeps delivering afterwards.
    const onQuote = vi.fn()
    const after = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create, onQuote })
    after.start()
    rig.latest().accept()
    rig.latest().deliver(quoteFrame())
    expect(onQuote).toHaveBeenCalledTimes(1)
    socket.stop()
    after.stop()
  })
})

/* -------------------------------------------------------------------------
 * Client → server
 * ---------------------------------------------------------------------- */

describe('the send path step 15 (b) uses', () => {
  it('sends the viewport hint as a markets_visible message', () => {
    const rig = harness()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create })
    socket.start()
    rig.latest().accept()

    expect(socket.sendMarketsVisible(['AAPL', 'MSFT'])).toBe(true)
    expect(rig.latest().messages).toEqual([
      { type: 'markets_visible', symbols: ['AAPL', 'MSFT'] },
    ])
    socket.stop()
  })

  it('never sends `subscribe` on connect — the default filter is everything', () => {
    const rig = harness()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create })
    socket.start()
    rig.latest().accept()

    expect(rig.latest().sent).toEqual([])
    socket.stop()
  })

  it('reports false rather than throwing when there is nothing open to say it on', () => {
    const rig = harness()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create })
    socket.start()

    expect(socket.sendMarketsVisible(['AAPL'])).toBe(false)
    socket.stop()
  })
})

/* -------------------------------------------------------------------------
 * Reconnect
 * ---------------------------------------------------------------------- */

describe('reconnect', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('backs off, doubling, and caps the delay', () => {
    let clock = 0
    const rig = harness(() => clock)
    const socket = new LiveSocket({
      url: 'ws://x/api/ws',
      create: rig.create,
      now: () => clock,
    })
    socket.start()
    rig.latest().accept()

    // A server that accepts and drops immediately must not be hammered at
    // the base delay forever, so the backoff only resets on a connection
    // that lasted.
    const delays: number[] = []
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const before = rig.sockets.length
      rig.latest().drop()
      expect(socket.status).toBe('reconnecting')
      const wait = RECONNECT_BASE_MS * 2 ** attempt
      const capped = Math.min(wait, RECONNECT_MAX_MS)
      delays.push(capped)
      vi.advanceTimersByTime(capped - 1)
      expect(rig.sockets).toHaveLength(before)
      vi.advanceTimersByTime(1)
      expect(rig.sockets).toHaveLength(before + 1)
      rig.latest().accept()
      clock += 1
    }

    expect(delays[0]).toBe(RECONNECT_BASE_MS)
    expect(delays[delays.length - 1]).toBe(RECONNECT_MAX_MS)
    socket.stop()
  })

  it('resets the backoff after a connection that lasted, so a restart is cheap', () => {
    let clock = 0
    const rig = harness(() => clock)
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create, now: () => clock })
    socket.start()
    rig.latest().accept()

    rig.latest().drop()
    vi.advanceTimersByTime(RECONNECT_BASE_MS)
    rig.latest().accept()

    clock += RECONNECT_STABLE_MS
    rig.latest().drop()
    const before = rig.sockets.length
    vi.advanceTimersByTime(RECONNECT_BASE_MS)
    expect(rig.sockets).toHaveLength(before + 1)
    socket.stop()
  })

  it('reopens after a server restart and replays the viewport hint the server forgot', () => {
    const rig = harness()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create })
    socket.start()
    rig.latest().accept()
    socket.sendMarketsVisible(['AAPL', 'MSFT'])

    rig.latest().drop()
    vi.advanceTimersByTime(RECONNECT_BASE_MS)
    rig.latest().accept()

    expect(rig.sockets).toHaveLength(2)
    expect(rig.latest().messages).toEqual([
      { type: 'markets_visible', symbols: ['AAPL', 'MSFT'] },
    ])
    socket.stop()
  })

  it('stays closed after a deliberate stop', () => {
    const rig = harness()
    const socket = new LiveSocket({ url: 'ws://x/api/ws', create: rig.create })
    socket.start()
    rig.latest().accept()

    socket.stop()
    vi.advanceTimersByTime(RECONNECT_MAX_MS * 4)

    expect(rig.sockets).toHaveLength(1)
    expect(socket.status).toBe('closed')
  })

  /** CLAUDE.md rule 9. A socket coming back up says nothing about whether
   * the engine is halted or about what the broker holds, and reconnecting
   * into an unverified position state is how a bot doubles a position it
   * already holds. */
  it('never resumes the engine, clears a halt, or writes engine state', () => {
    useUIStore.getState().halt()
    const before = useUIStore.getState()
    expect(before.isHalted).toBe(true)

    const rig = harness()
    const socket = new LiveSocket({
      url: 'ws://x/api/ws',
      create: rig.create,
      ...storeHandlers(),
    })
    socket.start()
    rig.latest().accept()
    rig.latest().drop()
    vi.advanceTimersByTime(RECONNECT_BASE_MS)
    rig.latest().accept()
    rig.latest().deliver(quoteFrame())

    const after = useUIStore.getState()
    expect(after.isHalted).toBe(true)
    expect(after.executionMode).toBe(before.executionMode)
    expect(after.accountMode).toBe(before.accountMode)
    // The reconnect did deliver a price, so this is not a vacuous pass.
    expect(after.quotes['AAPL']?.price).toBe(190)
    socket.stop()
  })

  /** The rule above is an *absence*, and an absence is what a
   * plausible-looking diff adds to — the same reason `tests/api/test_ws.py`
   * keeps a source-level check on the server half. */
  it('holds no reference to halting or resuming anywhere in the module', () => {
    // The length assertion is what fails if this ever reads nothing, so the
    // check below cannot pass vacuously.
    expect(liveSocketSource.length).toBeGreaterThan(1_000)
    const code = liveSocketSource
      .split('\n')
      .filter((line: string) => !/^\s*(\*|\/\*|\/\/)/.test(line))
      .join('\n')

    expect(code).not.toMatch(/\bresume\b/)
    expect(code).not.toMatch(/\bhalt/i)
    expect(code).not.toMatch(/isHalted/)
    expect(code).not.toMatch(/engineState/)
  })
})

/* -------------------------------------------------------------------------
 * The store wiring
 * ---------------------------------------------------------------------- */

describe('storeHandlers', () => {
  it('writes a priced quote into the live map and advances lastTickAt only', () => {
    const handlers = storeHandlers()
    expect(useUIStore.getState().lastTickAt).toBeNull()
    expect(useUIStore.getState().lastPollAt).toBeNull()

    handlers.onQuote?.(QUOTE)

    const s = useUIStore.getState()
    expect(s.quotes['AAPL']).toMatchObject({
      symbol: 'AAPL',
      price: 190,
      // The vendor's observation time, which is what the merge orders on —
      // not the arrival time.
      at: QUOTE.at,
      source: 'stream',
    })
    expect(s.lastTickAt).not.toBeNull()
    // Two feeds, two questions: a healthy stream must never make the poll
    // look alive.
    expect(s.lastPollAt).toBeNull()
  })

  it('leaves `underlyings` alone — the fixture map is not the live map', () => {
    const before = useUIStore.getState().underlyings['AAPL']
    storeHandlers().onQuote?.(QUOTE)
    expect(useUIStore.getState().underlyings['AAPL']).toBe(before)
  })

  it.each([
    ['no bid', { bid: null }],
    ['no ask', { ask: null }],
    ['crossed', { bid: 190.5, ask: 190.02 }],
  ])('writes nothing and stamps nothing for a quote with %s', (_label, patch) => {
    storeHandlers().onQuote?.({ ...QUOTE, ...patch })

    const s = useUIStore.getState()
    expect(s.quotes['AAPL']).toBeUndefined()
    expect(s.lastTickAt).toBeNull()
  })

  /** WEB-12. A priced frame whose `at` the merge cannot order reaches no
   * entry — `mergeQuotes` drops it, and from the stream it does not even
   * evict what is held (WEB-2). Stamping liveness for it is the *badge over
   * a frozen timestamp* this codebase says is worse than no badge: the map
   * took nothing from the frame, so a pill reading `Live` would be reporting
   * on a price that does not exist. Unobservable while `LiveStatus` renders
   * only against `lastNewsAt`; a stream pill returning to Markets or
   * Activity is what makes it visible, and that is too late to find it. */
  describe('a frame the map will not hold stamps no liveness', () => {
    it.each([
      ['an absent stamp', undefined as unknown as string],
      ['an unparseable stamp', 'not a time'],
    ])('writes nothing and stamps nothing for a priced quote with %s', (_label, at) => {
      storeHandlers().onQuote?.({ ...QUOTE, at })

      const s = useUIStore.getState()
      expect(s.quotes['AAPL']).toBeUndefined()
      expect(s.lastTickAt).toBeNull()
    })

    it('leaves a held entry from the other writer in place, and still stamps nothing', () => {
      // The WEB-2 asymmetry, seen from the socket: the frame is evidence
      // about the frame, so the polled entry survives — and the pill must
      // not claim the stream refreshed it.
      useUIStore.getState().applyPolledQuotes([
        liveFromStockQuote({
          symbol: 'AAPL',
          name: 'Apple Inc.',
          price: 188,
          at: '2026-09-17T14:29:00Z',
          previousClose: 187,
          volume: 1_000,
          volumeSession: 'in_progress',
          volumeDate: '2026-09-17',
          avgVolume: 2_000,
          marketCap: 3_000,
        }),
      ])
      const stamped = '2026-09-17T14:29:01Z'
      useUIStore.getState().markPolled(stamped)

      storeHandlers().onQuote?.({ ...QUOTE, at: undefined as unknown as string })

      const s = useUIStore.getState()
      expect(s.quotes['AAPL'].price).toBe(188)
      expect(s.quotes['AAPL'].source).toBe('poll')
      expect(s.lastTickAt).toBeNull()
      // The poll's own stamp is untouched: two feeds, two questions.
      expect(s.lastPollAt).toBe(stamped)
    })

    it('still stamps for an ordinary priced frame, on the arrival clock', () => {
      // The other direction, so the gate cannot pass by refusing everything.
      // `markStreamed` takes the *arrival* time deliberately: the Basic
      // plan's `indicative` options feed is 15 minutes delayed, and a pill
      // fed the vendor's stamp would read `stale` on a healthy socket.
      storeHandlers().onQuote?.(QUOTE)

      const s = useUIStore.getState()
      expect(s.quotes['AAPL'].price).toBe(190)
      expect(s.lastTickAt).not.toBeNull()
      expect(s.lastTickAt).not.toBe(QUOTE.at)
    })
  })

  it('records a trade update without moving a position or the ledger', () => {
    const positions = useUIStore.getState().openPositions.paper
    const activity = useUIStore.getState().activity.paper

    storeHandlers().onTradeUpdate?.({
      event: 'fill',
      at: '2026-09-17T14:31:00Z',
      orderId: 'ord-1',
      symbol: 'AAPL251219C00190000',
      status: 'filled',
      action: 'BTO',
      quantity: 2,
      filledQuantity: 2,
      fillPrice: 3.4,
      fillQuantity: 2,
      filledAvgPrice: 3.4,
      positionQuantity: 2,
    })

    const s = useUIStore.getState()
    expect(s.lastTradeUpdate?.orderId).toBe('ord-1')
    expect(s.openPositions.paper).toBe(positions)
    expect(s.activity.paper).toBe(activity)
    // A trade update is not a price.
    expect(s.lastTickAt).toBeNull()
  })

  it('records a stated refusal rather than dropping it', () => {
    storeHandlers().onError?.({ code: 'subscription_refused', message: 'the bound is 64.' })

    const s = useUIStore.getState()
    expect(s.lastStreamError).toEqual({
      code: 'subscription_refused',
      message: 'the bound is 64.',
    })
    // A refusal is this connection being told what it did wrong. It is not
    // engine state and must never read as a halt.
    expect(s.isHalted).toBe(initialState.isHalted)
  })
})
