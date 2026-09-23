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
 * **What that costs, and where it is paid — finding WEB-11.** Because
 * `readyState` starts at `CONNECTING`, `LiveSocket.send` returns `false` in
 * every suite that does not call {@link RecordingWebSocket.driveOpen}, and
 * the frame never reaches this stub at all. The composition that would meet
 * it — the Markets viewport hint travelling over a real `LiveSocket` — is
 * **tested only in parts today**, deliberately: `Markets.test.tsx` mocks
 * `sendMarketsVisible` at the module boundary, because what step 15 (b) owns
 * is *which* symbols are named and *how often*, while `liveSocket.test.ts`
 * and `useLiveSocket.test.tsx` own the frame and the connection over a
 * socket they drive themselves. Covered by parts is not covered by
 * composition, so the day those two meet, the failure has to be legible:
 * {@link RecordingWebSocket.sent} throws on a socket nobody opened, and
 * {@link RecordingWebSocket.send} throws on a frame the browser would have
 * refused. Neither invents an open socket — a `send` that reported success
 * from `CONNECTING` would make the vacuous pass worse, not better.
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
  private readonly frames: string[] = []
  /** Whether {@link driveOpen} was ever called. Not `readyState`: a socket
   * that opened and then closed *could* have carried frames, and one that
   * never opened could not have. */
  private everOpened = false
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

  /** Every frame the client sent, in order — the viewport hint and nothing
   * else, on today's client.
   *
   * **Throws on a socket that was never driven open — finding WEB-11.**
   * `readyState` starts at `CONNECTING` and only {@link driveOpen} moves it,
   * so `LiveSocket.send` refuses every frame before it reaches this stub:
   * the list is `[]` whatever the client did. A test asserting *the hint
   * went out* would then fail for a reason that reads like a product bug,
   * and one asserting *nothing was sent* would pass vacuously. Throwing
   * makes both loud and names the cause. Nothing is lost by it: *no frame
   * before open* is already structural, because {@link send} below refuses
   * to record one. */
  get sent(): readonly string[] {
    if (!this.everOpened) {
      throw new Error(
        'RecordingWebSocket: this socket was never driven open, so ' +
          'LiveSocket refused every frame before it reached the stub and ' +
          '`sent` is [] whatever the client did. Call driveOpen() before ' +
          'asserting on what was or was not sent.',
      )
    }
    return this.frames
  }

  send(data: string): void {
    if (this.readyState !== RecordingWebSocket.OPEN) {
      throw new Error(
        `RecordingWebSocket: send() from readyState ${this.readyState}. A ` +
          'real WebSocket throws InvalidStateError from CONNECTING and ' +
          'discards silently from CLOSED; both are loud here rather than ' +
          'appended, because a frame the browser would have dropped must ' +
          'not read back as one that went out. `LiveSocket.send` guards on ' +
          '`connected`, so this firing means that guard regressed.',
      )
    }
    this.frames.push(data)
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
    this.everOpened = true
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
