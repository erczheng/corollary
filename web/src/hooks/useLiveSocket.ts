import { useEffect } from 'react'
import { startLiveSocket } from '../lib/liveSocket'

/** Mount the app's one connection to `/api/ws` — step 15 (c).
 *
 * (a) built the client and (b) built the viewport hint that rides on it;
 * until this hook existed nothing called {@link startLiveSocket}, so the
 * socket was written, tested and never opened in the running app. This is
 * the whole of the mount: no arguments, no state, no return value.
 *
 * ## Mounted once, at the shell
 *
 * `AppShell` in `App.tsx`, beside `useMarketPoll` — the established shape
 * here for a side effect whose lifetime is the app's. **Never per page.**
 * The socket is a module singleton for the same reason `useMarketPoll`'s
 * interval is: one connection per browser is an app-wide invariant that no
 * component can hold. A second mount would be a second connection and two
 * viewport hints taking turns overwriting each other, and the server drops
 * a hint with the connection that sent it.
 *
 * ## No cleanup, deliberately
 *
 * The effect returns nothing, and that is a decision rather than an
 * omission.
 *
 * Ask what unmounting `AppShell` *means*. In the terminal it means the
 * React root is being torn down, which happens on page unload and nowhere
 * else — and the browser closes the socket itself there. The only time the
 * cleanup would actually run against a live app is React's StrictMode
 * double-invoke in development: effect, cleanup, effect. With no cleanup
 * that sequence is start → nothing → start, and the second `start()` is a
 * no-op (`if (!this.stopped) return`) against the same instance, so
 * development gets exactly one connection.
 *
 * `return stopLiveSocket` was the other defensible answer and is rejected
 * for what StrictMode does to it. `stopLiveSocket()` closes the socket
 * **and nulls the singleton**, so the second effect builds a *fresh*
 * `LiveSocket`: development opens, closes and reopens on every startup,
 * against a server that has to process the close and the new upgrade in
 * whatever order they arrive, and the new instance has forgotten the last
 * viewport hint — the one piece of state the reconnect path exists to
 * replay. A connection cycled once per boot is also exactly the kind of
 * noise that makes a reconnect log unreadable on the morning it matters.
 *
 * The cost of having no cleanup is confined to the test environment, where
 * unmounting is frequent and *does* mean "throw the app away": a stopped
 * mount would otherwise leave the singleton, and any reconnect timer it had
 * armed, alive for the next test. That is paid in `src/test/setup.ts`,
 * which calls `stopLiveSocket()` in the suite-wide `afterEach` — harness
 * hygiene belongs in the harness, not in the shape of the app's lifetime.
 * `useLiveSocket.test.tsx` pins both halves.
 *
 * ## What this does not do
 *
 * - **It does not touch the engine (rule 9).** Nothing on this path
 *   resumes, clears a halt, restarts anything a halt stopped, or writes
 *   engine state; `storeHandlers()` writes quotes, `lastTickAt`, trade
 *   updates and stated refusals — four fields, and nothing else. Engine state and notifications stay on their
 *   poll — a socket open must never invalidate them, because a refetch
 *   triggered by a reconnect is a reconnect reading as a resume.
 * - **It runs regardless of halt state.** A halt stops new entries; it does
 *   not stop marking what is held (rule 7), and neither does a flatten.
 *   There is no halt check here and there should not be one.
 * - **It changes what nothing renders (rule 4).** Every Markets row is
 *   polled anyway and a symbol with no streamed entry renders its polled
 *   row, so a refused hint or a dead socket costs freshness, never a frozen
 *   row.
 * - **It carries no credential (rule 6).** `liveSocketUrl()` derives host
 *   and scheme from the page, names no host, and carries no token and no
 *   query string. Nothing here logs. */
export function useLiveSocket(): void {
  useEffect(() => {
    startLiveSocket()
  }, [])
}
