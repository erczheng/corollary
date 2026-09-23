import { describe, it, expect } from 'vitest'
import { RecordingWebSocket, recordedSockets } from './recordingWebSocket'

const URL = 'ws://127.0.0.1:5173/api/ws'
const HINT = '{"type":"markets_visible","symbols":["NVDA"]}'

/** **Finding WEB-11, made loud rather than left as a note.**
 *
 * `readyState` starts at `CONNECTING` and only `driveOpen()` moves it, so in
 * every suite that does not drive the socket `LiveSocket.send` returns
 * `false` and no frame ever reaches the stub. A future test asserting *the
 * hint went out* would fail for a reason that reads like a product bug, and
 * one asserting *nothing was sent* would pass vacuously. The composition
 * that would meet this — step 15 (b)'s viewport hint travelling over (c)'s
 * real socket — is covered only in parts today, `Markets.test.tsx` mocking
 * `sendMarketsVisible` at the module boundary while `useLiveSocket.test.tsx`
 * drives a socket with no Markets page mounted.
 *
 * So the stub states the condition instead of absorbing it. Neither throw
 * invents an open socket: a `send` that reported success from `CONNECTING`
 * would make the vacuous pass worse. */
describe('the recording WebSocket', () => {
  it('is the socket the run sees, and records every construction', () => {
    const socket = new WebSocket(URL)

    // `setup.ts` installs it globally, which is what keeps twelve suites
    // rendering `<App />` from opening a real connection and arming a
    // reconnect timer inside the run.
    expect(socket).toBeInstanceOf(RecordingWebSocket)
    expect(recordedSockets()).toHaveLength(1)
    expect(recordedSockets()[0].url).toBe(URL)
  })

  it('refuses to report frames on a socket nobody opened', () => {
    const socket = new RecordingWebSocket(URL)

    // The vacuous pass, turned into a failure that names its own cause.
    expect(() => socket.sent).toThrow(/never driven open/)

    socket.driveOpen()
    expect(socket.sent).toEqual([])
  })

  it('records a frame on an open socket, and refuses one the browser would have', () => {
    const socket = new RecordingWebSocket(URL)

    // A real WebSocket throws InvalidStateError from CONNECTING. Recording
    // the frame instead would report a message as sent that the browser
    // would never have put on the wire.
    expect(() => socket.send(HINT)).toThrow(/readyState 0/)

    socket.driveOpen()
    socket.send(HINT)
    expect(socket.sent).toEqual([HINT])

    // A real WebSocket discards silently from CLOSED, which is exactly the
    // mask this file exists to avoid, so it is loud here too — and the
    // frame is still not recorded.
    socket.close()
    expect(() => socket.send(HINT)).toThrow(/readyState 3/)
    expect(socket.sent).toEqual([HINT])
  })
})
