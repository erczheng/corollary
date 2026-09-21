import { SOCKET_OPEN, type SocketLike } from '../lib/liveSocket'

/** The `WebSocket` the whole Vitest run sees — **a suppression, not a gap.**
 *
 * That distinction is the whole reason this file has a docstring. The
 * `IntersectionObserver` in `setup.ts` fills a genuine hole: jsdom implements
 * no layout, so it ships no observer at all and never could. jsdom *does*
 * implement `WebSocket`, and implements it properly — so this stub
 * **suppresses a real connection** rather than standing in for a missing API.
 * Left alone, every suite that renders `<App />` (twelve of them) would open
 * `ws://localhost/api/ws` for real against a server that is not running, take
 * the close, and schedule a 500ms reconnect inside the test run.
 *
 * **Recording rather than inert**, which is finding WEB-9 turned around. The
 * inert observer "masks in one direction: a future component that uses it to
 * decide *is this visible* takes the never-visible branch forever, so a test
 * asserting **absence** passes vacuously." An inert socket would mask the
 * same way — `useLiveSocket.test.tsx` could assert "nothing connected" and be
 * right for the wrong reason forever. Recording turns that hazard into the
 * assertion: the tests state that the mount happened, and happened *once*.
 *
 * **Nothing happens here on its own.** No open, no error, no frame, no timer;
 * `readyState` stays `CONNECTING` until a test says otherwise. The `drive*`
 * methods are the only way anything moves, and they are named so that a grep
 * separates what a test caused from what the client did.
 *
 * Typed `implements SocketLike` rather than `implements WebSocket`:
 * `SocketLike` is the entire surface `LiveSocket` uses, so the compiler checks
 * the contract that is actually exercised instead of a hundred DOM members
 * nothing calls. The one cast to `typeof WebSocket` lives at the install site
 * in `setup.ts`, where it is visible. */
export class RecordingWebSocket implements SocketLike {
  /** `WebSocket.CONNECTING`, `.OPEN` and `.CLOSED`, spelled out for the same
   * reason `liveSocket.ts` spells out {@link SOCKET_OPEN}. */
  static readonly CONNECTING = 0
  static readonly OPEN = SOCKET_OPEN
  static readonly CLOSED = 3

  readonly url: string
  /** Mutable so a test can drive it; the client only ever reads it. */
  readyState: number = RecordingWebSocket.CONNECTING
  /** Every frame the client sent, in order — the viewport hint and nothing
   * else, on today's client. */
  readonly sent: string[] = []
  /** How many times the client closed this socket deliberately. */
  closeCount = 0

  onopen: ((event: unknown) => void) | null = null
  onclose: ((event: unknown) => void) | null = null
  onerror: ((event: unknown) => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null

  constructor(url: string) {
    this.url = url
    constructions.push(this)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closeCount += 1
    this.readyState = RecordingWebSocket.CLOSED
    // No `onclose`. A stub that called its own close handler would schedule
    // the client's reconnect from inside a teardown, which is the timer this
    // file exists to keep out of the run.
  }

  /* --- test drivers: nothing below is reachable from production code --- */

  /** The server accepted the connection. */
  driveOpen(): void {
    this.readyState = RecordingWebSocket.OPEN
    this.onopen?.({})
  }

  /** The connection dropped. This is the one driver that arms the client's
   * reconnect, so a test calling it owns the timers that follow. */
  driveClose(): void {
    this.readyState = RecordingWebSocket.CLOSED
    this.onclose?.({})
  }

  /** One frame from the server, already serialised the way the wire carries
   * it. */
  driveMessage(data: unknown): void {
    this.onmessage?.({ data })
  }
}

const constructions: RecordingWebSocket[] = []

/** Every socket constructed since the last reset, oldest first.
 *
 * A reconnect appends: index 0 is the first attempt, the last entry is the
 * current one. */
export function recordedSockets(): readonly RecordingWebSocket[] {
  return constructions
}

/** Forget the record. Called from the suite-wide `afterEach` so a count in
 * one test is never a count of another test's mounts. */
export function resetRecordedSockets(): void {
  constructions.length = 0
}
