/**
 * The browser's `/api/ws` client — step 15 (a).
 *
 * Connect, read the three-kind server frame contract, route quotes into the
 * one live quote map, and reconnect when the engine restarts. Pure enough to
 * test without rendering anything: the socket itself is injected, so
 * `liveSocket.test.ts` drives frames, closes and clocks directly the way
 * `orders.ts` and `markets.ts` are driven.
 *
 * ## Three kinds, read off the discriminant and never sniffed
 *
 * `corollary/api/schemas.py` tags every server frame with `type` and nests
 * its payload under a key named after the tag — `quote`, `update`, `error` —
 * deliberately, and the reason is written down there: *"a client that has to
 * infer the kind from which fields are present infers wrongly the first time
 * a payload gains a nullable field, and one of these kinds is about money."*
 * So {@link parseServerFrame} switches on `type` and reads exactly one key.
 * A payload missing under its own tag is a **missing key**, reported as
 * unreadable, rather than a plausible object with absent fields.
 *
 * **An unknown `type` is neither a crash nor a guess.** It is reported as
 * `unknown` and dropped. A server one version ahead is the ordinary cause,
 * and the worst available answer would be to fall back to field-sniffing it.
 *
 * **It is not counted, and that is a gap rather than a design.** This line
 * claimed it was; nothing in this module holds a counter, and
 * {@link storeHandlers} wires no `onUnusable`, so in the app as assembled an
 * unknown or unreadable frame leaves no trace at all. The claim is corrected
 * rather than the code because a counter wants somewhere to be read, and
 * nothing renders one yet. This is **not** rule 8: a rejected order's record
 * is the engine's structured log plus the 15s activity poll, both
 * server-side, and no rejection reaches the unreadable branch by virtue of
 * being a rejection — `event` and `status` are free strings and route fine.
 *
 * ## What is deliberately *not* on this socket
 *
 * Engine halt state and notifications, which are polled at 15s. Rule 9 halts
 * *because* the socket closed, so a halt announcement cannot ride the thing
 * whose closure caused the halt; and a broken **client** socket has to stay
 * distinguishable from a **halted engine**, or a human presses Resume on an
 * engine nobody halted. A fourth server frame kind is a decision, not an
 * addition — nothing here asks for one, and nothing here would read one.
 *
 * ## Rule 9, at the one place it is tempting to break
 *
 * **Reconnecting never resumes anything.** Nothing in this module calls
 * `resume()`, clears a halt, or writes any engine state at all; the only
 * store fields it touches are the live quote map, its own liveness stamp,
 * the last trade update and the last stated refusal. Reconnecting into an
 * unverified position state is how a bot doubles a position it already
 * holds, and a socket coming back up is exactly the moment that temptation
 * appears in the code. Recovery from a halt is an explicit human action,
 * and `liveSocket.test.ts` pins that a reconnect leaves `isHalted` alone.
 *
 * Related, and recorded in `routes/ws.py`'s own docstring: the browser half
 * arms nothing on the watchdog. A tab opening or closing says nothing about
 * whether Alpaca is connected.
 *
 * ## Why this client does not send `subscribe` on connect
 *
 * `subscribe` is a delivery filter **on this connection only** — it does not
 * reach the vendor sockets, so it can never make anything stream that is not
 * streaming already. It can only *narrow* what this connection is delivered.
 * A fresh `Subscription` starts at `self._symbols = None`
 * (`corollary/api/fanout.py`), and `wants()` returns `True` for every frame
 * while it is `None`: **a client that never sends `subscribe` is delivered
 * every symbol the fan-out publishes**, which is already the set the engine's
 * prioritised budget chose. Sending `{"symbols": null}` on connect would ask
 * for precisely that and cost a round trip to say nothing; sending a concrete
 * list would mean maintaining a second copy of "what this app cares about"
 * — positions, chain rows, the Markets viewport — that goes out of date
 * silently, and a filter that has fallen behind drops a held position's mark
 * with no error anywhere (there is no acknowledgement frame, so silence is
 * success). The filter stays unset until something has an actual reason to
 * narrow it.
 */

import { API_BASE, errorBodyOf } from './api'
import { useUIStore } from './store'
import type {
  ApiErrorBody,
  WsClientMessage,
  WsQuote,
  WsTradeUpdate,
} from './types'

/* -------------------------------------------------------------------------
 * Where the socket is
 * ---------------------------------------------------------------------- */

/** The websocket URL, derived from whatever base `api.ts` already uses.
 *
 * No host is ever named here. `API_BASE` is relative (`/api`) by default —
 * Vite proxies it in development and the built bundle is served from the
 * API's own origin — so the host comes from the page, and the scheme follows
 * it: `https:` pages take `wss:` and everything else takes `ws:`. The
 * terminal binds to loopback and must work with no internet at all, and a
 * hardcoded `127.0.0.1:8000` would break the moment either half moved.
 *
 * `VITE_API_BASE` may be absolute, for the case where the engine runs
 * somewhere the dev server does not proxy to; resolving through `URL` keeps
 * that case working and swaps the scheme rather than the authority. A base
 * already spelled `ws:`/`wss:` is left as it is. */
export function liveSocketUrl(base: string = API_BASE, pageHref?: string): string {
  const href =
    pageHref ?? (typeof window === 'undefined' ? 'http://127.0.0.1/' : window.location.href)
  const url = new URL(`${base}/ws`, href)
  if (url.protocol === 'https:') url.protocol = 'wss:'
  else if (url.protocol === 'http:') url.protocol = 'ws:'
  return url.toString()
}

/* -------------------------------------------------------------------------
 * The midpoint, which is a derivation and not a wire field
 * ---------------------------------------------------------------------- */

/** The midpoint of a two-sided quote, or null when there is not one.
 *
 * **Mirrors `Quote.mid` on the server exactly**, because there is no `mid`
 * on the wire and inventing a second rule for it is how the two halves start
 * disagreeing about what a quote is worth. Three branches:
 *
 * - **Either side null → null.** Alpaca sends `0` for an empty side and the
 *   server maps that to `None`; one side of a book is not a price.
 * - **Crossed (`bid > ask`) → null.** A bid above an ask is a data error,
 *   not a tradeable market, and averaging the two produces a confident
 *   number for a market that does not exist.
 * - Otherwise `(bid + ask) / 2`. A locked book (`bid === ask`) is ordinary
 *   and prices fine.
 *
 * **No mid means no price, and no price means no write into the merge** —
 * the server says the same thing on its side of the wire (`markets._spot`:
 * a one-sided or crossed quote prices nothing and therefore stamps nothing).
 *
 * The non-finite guard is not decoration: frames arrive as unvalidated JSON
 * and `NaN > NaN` is `false`, so without it a `NaN` bid would fall through
 * the crossed branch and be published as a price. */
export function midOf(bid: number | null | undefined, ask: number | null | undefined): number | null {
  if (bid == null || ask == null) return null
  if (!Number.isFinite(bid) || !Number.isFinite(ask)) return null
  if (bid > ask) return null
  return (bid + ask) / 2
}

/* -------------------------------------------------------------------------
 * Reading a frame
 * ---------------------------------------------------------------------- */

/** One server frame, as this client understands it.
 *
 * `unknown` and `unreadable` are stated outcomes rather than exceptions: a
 * live feed that throws on a frame it did not expect is a live feed that
 * stops on the first one. */
export type ParsedFrame =
  | { kind: 'quote'; quote: WsQuote }
  | { kind: 'trade_update'; update: WsTradeUpdate }
  | { kind: 'error'; error: ApiErrorBody }
  /** A `type` this client has never heard of. Carries the tag so a caller
   * can say what it ignored; `null` when the tag was not even a string. */
  | { kind: 'unknown'; type: string | null }
  /** Not JSON, not an object, or the payload absent under its own tag. */
  | { kind: 'unreadable'; reason: string }

function payloadOf(frame: Record<string, unknown>, key: string): Record<string, unknown> | null {
  const value = frame[key]
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null
}

/** Read one text frame. **Dispatches on `type` and nothing else.**
 *
 * The payload is then read from the single key named after the tag, so a
 * frame whose shape has drifted fails as a missing key here rather than
 * becoming a plausible object full of `undefined` three layers down — where
 * one of the three kinds is about money.
 *
 * Fields inside a payload are *not* re-validated, exactly as `request<T>`
 * casts rather than validates on the HTTP side: the wire contract is pinned
 * by `tests/api/test_ws.py`, and a second schema on this side is a second
 * thing to keep in step. What is checked is everything a wrong *kind* would
 * turn into a silent misread. */
export function parseServerFrame(data: unknown): ParsedFrame {
  if (typeof data !== 'string') {
    return { kind: 'unreadable', reason: 'the frame was not text' }
  }

  let body: unknown
  try {
    body = JSON.parse(data)
  } catch {
    return { kind: 'unreadable', reason: 'the frame did not parse as JSON' }
  }

  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    return { kind: 'unreadable', reason: 'a server frame is a tagged JSON object' }
  }

  const frame = body as Record<string, unknown>
  const type = frame['type']
  if (typeof type !== 'string') return { kind: 'unknown', type: null }

  switch (type) {
    case 'quote': {
      const quote = payloadOf(frame, 'quote')
      if (quote === null) return { kind: 'unreadable', reason: 'a quote frame carried no `quote`' }
      return { kind: 'quote', quote: quote as unknown as WsQuote }
    }
    case 'trade_update': {
      const update = payloadOf(frame, 'update')
      if (update === null) {
        return { kind: 'unreadable', reason: 'a trade_update frame carried no `update`' }
      }
      return { kind: 'trade_update', update: update as unknown as WsTradeUpdate }
    }
    case 'error': {
      // `api.ts`'s parser, not a second one. The socket's error body is the
      // same `ApiErrorBody` in the same position as the HTTP envelope's,
      // and a client holding two error parsers uses the wrong one on the
      // day it matters.
      const error = errorBodyOf(frame)
      if (error === null) {
        return { kind: 'unreadable', reason: 'an error frame carried no stated condition' }
      }
      return { kind: 'error', error }
    }
    default:
      return { kind: 'unknown', type }
  }
}

/* -------------------------------------------------------------------------
 * The socket
 * ---------------------------------------------------------------------- */

/** What this client needs of a `WebSocket`. Injected so the tests never open
 * one: jsdom's implementation would want a server, and a live feed's
 * reconnect behaviour is the part most worth testing deterministically. */
export interface SocketLike {
  readonly readyState: number
  send(data: string): void
  close(): void
  onopen: ((event: unknown) => void) | null
  onclose: ((event: unknown) => void) | null
  onerror: ((event: unknown) => void) | null
  onmessage: ((event: { data: unknown }) => void) | null
}

/** `WebSocket.OPEN`, spelled out rather than read off the global: this module
 * must be importable in an environment that has no `WebSocket` at all. */
export const SOCKET_OPEN = 1

export type LiveSocketStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed'

/** The first retry delay. Short enough that a `--reload` restart is invisible. */
export const RECONNECT_BASE_MS = 500
/** The ceiling on the retry delay. The engine may be down for an hour; the
 * browser keeps asking, once every fifteen seconds, forever.
 *
 * **Bounded in delay, not in attempts.** Giving up would leave a terminal
 * that has to be reloaded by hand to come back — and worse, a permanently
 * frozen price map behind a header that has no way to say why. The staleness
 * of the data is what the status pill reports; the socket's job is to keep
 * trying. */
export const RECONNECT_MAX_MS = 15_000
/** How long a connection must last before it counts as recovered.
 *
 * Without this, a server that accepts and immediately drops — a restart loop,
 * a proxy with nothing behind it — would reset the backoff on every open and
 * the client would hammer it at the base delay indefinitely. */
export const RECONNECT_STABLE_MS = 10_000

export interface LiveSocketHandlers {
  onQuote?: (quote: WsQuote) => void
  onTradeUpdate?: (update: WsTradeUpdate) => void
  /** A stated refusal. **Not fatal and not a close** — a rejected
   * subscription that closed the socket would look exactly like a dead
   * connection, which is the confusion the frame contract exists to
   * prevent. */
  onError?: (error: ApiErrorBody) => void
  /** A `type` this client does not know, or a frame it could not read.
   * Reported rather than thrown, and dropped rather than guessed. */
  onUnusable?: (frame: ParsedFrame) => void
  onStatus?: (status: LiveSocketStatus) => void
}

export interface LiveSocketOptions extends LiveSocketHandlers {
  url?: string
  /** How to open a socket. Defaults to the platform `WebSocket`. */
  create?: (url: string) => SocketLike
  now?: () => number
}

/** One connection to `/api/ws`, with the reconnect around it.
 *
 * Deliberately not a hook and not a store slice: the reconnect state machine
 * is the part worth testing, and it has nothing to do with React. The store
 * is written through its own actions by the handlers in
 * {@link storeHandlers}. */
export class LiveSocket {
  readonly url: string
  private readonly create: (url: string) => SocketLike
  private readonly now: () => number
  private readonly handlers: LiveSocketHandlers

  private socket: SocketLike | null = null
  private timer: ReturnType<typeof setTimeout> | null = null
  private delayMs = RECONNECT_BASE_MS
  private openedAt: number | null = null
  private stopped = true
  private state: LiveSocketStatus = 'idle'

  /** The last viewport hint sent, replayed on every reconnect.
   *
   * The server drops a connection's hint when the connection goes
   * (`_forget_viewport`), so a hint sent once and never repeated is a hint
   * that silently stops applying after the first restart. Remembering the
   * last one is this client's business; *deciding* what is on screen,
   * debouncing it and diffing it is step 15 (b)'s, and none of that is
   * here. */
  private viewport: string[] | null = null

  constructor(options: LiveSocketOptions = {}) {
    this.url = options.url ?? liveSocketUrl()
    this.create = options.create ?? ((url) => new WebSocket(url) as unknown as SocketLike)
    this.now = options.now ?? (() => Date.now())
    this.handlers = options
  }

  get status(): LiveSocketStatus {
    return this.state
  }

  /** Whether a client message sent right now would reach the server. */
  get connected(): boolean {
    return this.socket !== null && this.socket.readyState === SOCKET_OPEN
  }

  /** Open the connection, and keep it open. Idempotent. */
  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.delayMs = RECONNECT_BASE_MS
    this.open()
  }

  /** Close deliberately, and stay closed. The one path that does not
   * reconnect — a caller asking for the feed to stop is not a failure. */
  stop(): void {
    this.stopped = true
    this.clearTimer()
    const socket = this.socket
    this.socket = null
    if (socket) {
      detach(socket)
      try {
        socket.close()
      } catch {
        // Already closing or already gone. Nothing to recover.
      }
    }
    this.setStatus('closed')
  }

  /** Send one client message. `false` means it did not go out **now**.
   *
   * There is no acknowledgement frame, so a `true` here means the message
   * reached the socket and nothing more: silence from the server means it
   * landed, and a refusal arrives as an ordinary `error` frame with code
   * `subscription_refused`, on a socket that stays open. */
  send(message: WsClientMessage): boolean {
    if (!this.connected || this.socket === null) return false
    try {
      this.socket.send(JSON.stringify(message))
      return true
    } catch {
      // The socket closed between the readyState read and the send. The
      // close handler is what reconnects; this is only a report.
      return false
    }
  }

  /** **The send path step 15 (b) uses.** Name the equity tickers currently on
   * screen in the Markets table.
   *
   * This client sends what it is given, remembers it for the next reconnect,
   * and nothing else. It does not observe the viewport, does not debounce,
   * and does not diff — (b) owns all three, and the spec is explicit that the
   * hint is sent debounced and only when the set actually differs, because
   * every resubscribe is a gap in the marks.
   *
   * Refusals arrive asynchronously through `onError` with code
   * `subscription_refused`: over-long (the bound is 64), an OCC contract
   * where an equity ticker belongs, a malformed ticker, or the engine's own
   * narrower validation. A `true` return is not an acceptance. */
  sendMarketsVisible(symbols: readonly string[]): boolean {
    this.viewport = [...symbols]
    return this.send({ type: 'markets_visible', symbols: [...symbols] })
  }

  /* ---------------------------------------------------------------- */

  private open(): void {
    this.setStatus(this.delayMs === RECONNECT_BASE_MS ? 'connecting' : 'reconnecting')
    let socket: SocketLike
    try {
      socket = this.create(this.url)
    } catch {
      // A constructor that throws is a bad URL or a blocked scheme. Treat it
      // as a failed connection so the backoff still applies rather than
      // spinning.
      this.scheduleReconnect()
      return
    }

    this.socket = socket
    socket.onopen = () => {
      if (this.socket !== socket) return
      this.openedAt = this.now()
      this.setStatus('open')
      // The server forgot this connection's hint when the last one closed.
      if (this.viewport !== null) this.send({ type: 'markets_visible', symbols: this.viewport })
    }
    socket.onmessage = (event) => {
      if (this.socket !== socket) return
      this.receive(event.data)
    }
    // A browser fires `error` and then `close`; the close is what carries
    // the reconnect. Bound anyway so a stray error is not an unhandled one.
    socket.onerror = () => {}
    socket.onclose = () => {
      if (this.socket !== socket) return
      detach(socket)
      this.socket = null
      this.scheduleReconnect()
    }
  }

  private receive(data: unknown): void {
    const frame = parseServerFrame(data)
    switch (frame.kind) {
      case 'quote':
        this.handlers.onQuote?.(frame.quote)
        return
      case 'trade_update':
        this.handlers.onTradeUpdate?.(frame.update)
        return
      case 'error':
        // Stated, never fatal. The feed continues.
        this.handlers.onError?.(frame.error)
        return
      default:
        this.handlers.onUnusable?.(frame)
    }
  }

  /** Back off and try again — and **do nothing else**.
   *
   * Rule 9 lives here. A socket coming back up says nothing about whether
   * the engine is halted, whether the broker still holds what we think it
   * holds, or whether anything may be traded. This function schedules a
   * connection attempt. It does not resume, does not clear a halt and does
   * not write engine state. Recovery is a human action.
   *
   * **One thing the reconnect path does re-send, and it is deliberate:**
   * `open()`'s `onopen` replays the viewport hint, because the server drops
   * it when the connection that sent it closes. That is data recovery, not a
   * resume — the hint is the lowest subscription tier, and per the server's
   * `SubscriptionPlan.engine_subscribed` gate it can neither open a socket
   * nor arm a watchdog condition by itself, so a reconnecting browser cannot
   * cause or clear a rule-9 halt. The sentence above used to read "does not
   * re-request anything the server would treat as an instruction", which is
   * true of this function and false of the path a rule-9 reader traces from
   * it. */
  private scheduleReconnect(): void {
    if (this.stopped) {
      this.setStatus('closed')
      return
    }

    const lasted = this.openedAt === null ? 0 : this.now() - this.openedAt
    this.delayMs = lasted >= RECONNECT_STABLE_MS ? RECONNECT_BASE_MS : this.delayMs
    this.openedAt = null

    const wait = this.delayMs
    // Doubled for *next* time, so the first retry after a healthy connection
    // is immediate-ish and a server that never comes back is asked for once
    // every RECONNECT_MAX_MS. No jitter: there is one client on one machine,
    // so there is no herd to thunder, and a deterministic delay is a
    // deterministic test.
    this.delayMs = Math.min(this.delayMs * 2, RECONNECT_MAX_MS)
    this.setStatus('reconnecting')
    this.clearTimer()
    this.timer = setTimeout(() => {
      this.timer = null
      if (this.stopped) return
      this.open()
    }, wait)
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer)
      this.timer = null
    }
  }

  private setStatus(status: LiveSocketStatus): void {
    if (this.state === status) return
    this.state = status
    this.handlers.onStatus?.(status)
  }
}

function detach(socket: SocketLike): void {
  socket.onopen = null
  socket.onclose = null
  socket.onerror = null
  socket.onmessage = null
}

/* -------------------------------------------------------------------------
 * The store wiring
 * ---------------------------------------------------------------------- */

/** The handlers that write this socket into the app's state.
 *
 * Separated from {@link LiveSocket} so the transport can be tested without
 * the store and the store writes can be tested without a socket — and so
 * that what this feed is allowed to touch is one readable list:
 *
 * - a **priced** quote goes into the live map through `applyStreamedQuote`,
 *   which is `quotes.ts#streamedQuote` plus the merge. Never into
 *   `underlyings`, which ships pre-seeded with fixture prices — a server
 *   quote landing there would leave every never-polled symbol rendering an
 *   invented price indistinguishable from a real one;
 * - `markStreamed` stamps liveness, and **only a priced quote stamps it**;
 * - `applyTradeUpdate` records the broker's last word about an order. It
 *   moves no position and no money: the server is authoritative for both;
 * - `recordStreamError` records a stated refusal so it is visible rather
 *   than silent.
 *
 * Nothing here halts, resumes, or writes engine state. */
export function storeHandlers(): LiveSocketHandlers {
  return {
    onQuote: (quote) => {
      const price = midOf(quote.bid, quote.ask)
      // No mid is no price. A one-sided or crossed quote prices nothing and
      // therefore stamps nothing — the same sentence the server's `_spot`
      // is written to. The liveness stamp is withheld too: the pill answers
      // "is there a fresh price on screen", and there is not.
      if (price === null) return
      const store = useUIStore.getState()
      // `quote.at` is the **vendor's** observation time and is what the
      // merge orders two writers on.
      store.applyStreamedQuote(quote.symbol, price, quote.at)
      // The liveness stamp is the *arrival* time, which is a different
      // question and deliberately a different clock — see `markStreamed`.
      store.markStreamed(new Date().toISOString())
    },
    onTradeUpdate: (update) => {
      useUIStore.getState().applyTradeUpdate(update)
    },
    onError: (error) => {
      useUIStore.getState().recordStreamError(error)
    },
  }
}

/* -------------------------------------------------------------------------
 * The app's one socket
 * ---------------------------------------------------------------------- */

let current: LiveSocket | null = null

/** The app's live socket, or null before anything started one.
 *
 * Module-level because the invariant is app-wide, the same way
 * `useMarketPoll`'s interval registry is: one connection per browser, not
 * one per component that happens to want a price. Two sockets would double
 * the fan-out's work and give the server two viewport hints that take turns
 * overwriting each other. */
export function liveSocket(): LiveSocket | null {
  return current
}

/** Start the app's socket, or return the one already running.
 *
 * **Called from exactly one place:** `useLiveSocket` in
 * `web/src/hooks/useLiveSocket.ts`, mounted once in `AppShell` — step 15
 * (c). Until that hook existed this said *nothing calls this yet*, because
 * starting the feed decides what every page's liveness stamp means and had
 * to be sequenced against the Phase 1 fixture tick that wrote the same
 * field. That tick has no caller left in the app, so there is no second
 * writer to sequence against any more.
 *
 * **Idempotent, and the mount depends on it being so.** `start()` returns
 * early while the instance is running, so React StrictMode's
 * effect/cleanup/effect double-invoke yields one connection rather than
 * two — which is why `useLiveSocket` registers no cleanup. That reasoning
 * is written out there, not here. */
export function startLiveSocket(options: LiveSocketOptions = {}): LiveSocket {
  if (current === null) current = new LiveSocket({ ...storeHandlers(), ...options })
  current.start()
  return current
}

/** Stop and forget the app's socket. Deliberate, and does not reconnect. */
export function stopLiveSocket(): void {
  current?.stop()
  current = null
}

/** **The module-level send path step 15 (b) uses.**
 *
 * `false` means there is no open socket to say it on right now; the hint is
 * still remembered and replayed when one opens, provided the app's socket
 * exists. With no socket started at all nothing is remembered, which is the
 * honest answer to "tell the server what is on screen" when there is no
 * server connection. */
export function sendMarketsVisible(symbols: readonly string[]): boolean {
  return current?.sendMarketsVisible(symbols) ?? false
}
