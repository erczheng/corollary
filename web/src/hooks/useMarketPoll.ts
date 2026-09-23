import { useEffect } from 'react'
import { useQueryClient, type QueryClient } from '@tanstack/react-query'
import { refetchStocks } from '../lib/queries'
import { liveFromStockQuote } from '../lib/quotes'
import { useUIStore } from '../lib/store'

/** The Markets stock poll, in three states — decision 18.
 *
 * Hidden, foreground and background are **three** states, not two:
 *
 * - **Hidden** (`document.hidden`) — the poll *stops*. Not slowed to 5s:
 *   nothing is rendered, so a background tab at 5s is 12 requests a minute
 *   of work nobody can see, and the money-bearing numbers are on a
 *   server-side stream that does not care whether a tab is painted. On
 *   return the interval restarts and the first read fires immediately with
 *   it, in the same turn — the read never waits out an interval first — so
 *   a revisit never shows a screen of skeletons.
 * - **Foreground** — visible *and* the Markets route mounted:
 *   {@link MARKETS_FOREGROUND_POLL_MS}. Which page is open is read from the
 *   router, not from a store flag — `Markets.tsx` mounts this hook because
 *   `Markets.tsx` is what consumes the data. A flag set on navigation is a
 *   flag that survives a crash, a modal, or a route the author forgot.
 * - **Background** — visible, Markets not mounted:
 *   {@link MARKETS_BACKGROUND_POLL_MS}, from the one app-level mount. It
 *   exists so arriving at Markets renders a five-second-old table instead
 *   of skeletons, and so the server's session-volume and daily-series
 *   caches stay warm; at 12/min it is noise against the budget.
 *
 * **One hook, one interval.** Both mounts call this same hook and they
 * coordinate through the module-level registry below, so exactly one
 * `setInterval` exists at a time: Markets mounting supersedes the app-level
 * cadence and the app-level one resumes on unmount. Two hooks with two
 * timers would double the vendor rate for as long as both were mounted,
 * and that is the rate this whole design is rationing.
 *
 * ## Why this owns an interval instead of using `refetchInterval`
 *
 * Deliberate, and all three reasons are load-bearing:
 *
 * 1. **`refetchInterval` gates on window *focus*, not on `document.hidden`.**
 *    With the default `refetchIntervalInBackground: false` it pauses when the
 *    window loses focus — but a visible, unfocused window is exactly how a
 *    trading terminal is watched (beside an editor), and it is still painted,
 *    so it is still worth refreshing. Setting the flag `true` removes the
 *    gate entirely and polls a hidden tab, which is the state decision 18
 *    stops outright. Neither setting expresses "visible" and that is the axis
 *    the three states are defined on.
 * 2. **`refetchInterval` is per-observer.** The app-level mount and the
 *    Markets mount would each hold a timer at their own cadence, which is
 *    the one thing decision 18 rules out.
 * 3. **The background state has no observer at all.** Markets is unmounted,
 *    so nothing is subscribed to the stocks query — and a refetch interval
 *    that only runs while something renders the data cannot warm a cache for
 *    the page that is not open yet.
 *
 * `refetchOnWindowFocus` is a separate question and is left at its default:
 * it only refetches a *stale* query, which after a focus round-trip is the
 * right answer and is deduped against whatever this poll has in flight.
 *
 * Siblings, not parameters on each other: `useLiveTick` and `useNewsPoll`
 * stand for different mechanisms with different budgets — the websockets are
 * capped per asset class on the Basic plan (30 equity symbols, 200 option
 * quotes) and are spent on open positions, while a page of chains does not
 * fit and is covered by snapshot requests under a 200/min budget instead. */

/** The foreground interval, in milliseconds.
 *
 * **The server holds this same number under this same name** —
 * `MARKETS_FOREGROUND_POLL_MS` in `corollary/api/routes/markets.py`, where
 * `STOCK_SNAPSHOT_TTL` is derived from it. That equality is the design, not
 * a coincidence: `GET /api/markets/stocks` coalesces every caller inside one
 * TTL into a single Alpaca request, so N tabs cost one vendor request per
 * interval rather than N — a claim that is only true while the two halves
 * are equal. Grep `MARKETS_FOREGROUND_POLL_MS` to find both. **Move them
 * together or not at all**; `useMarketPoll.test.tsx` pins this side and
 * `test_the_cache_ttl_is_the_foreground_poll_interval` pins the other.
 *
 * 400 is a floor, not a preference. It is 150/min of a 200/min bucket,
 * leaving ~49/min for on-demand chains. 300ms is exactly at the ceiling with
 * nothing left; 150ms (400/min) and 200ms (300/min) were asked for and not
 * adopted, because `ratelimit.py`'s bucket *waits* rather than refusing, so
 * exceeding it would surface as latency creep that looks like it worked.
 *
 * **And it is enforced as a floor, not observed as one:** `subscribe` clamps
 * every requested interval up to this number, so a third call site written
 * as `useMarketPoll(200)` polls at 400 rather than winning the registry. */
export const MARKETS_FOREGROUND_POLL_MS = 400

/** The background interval: visible, but Markets is not the open page.
 *
 * 12 requests a minute, which is noise against the budget, and it buys a
 * table that is five seconds old rather than empty on arrival. */
export const MARKETS_BACKGROUND_POLL_MS = 5_000

/* -------------------------------------------------------------------------
 * The registry — one interval for the whole app
 *
 * Module-level because the invariant is app-wide: "exactly one interval
 * exists at a time" is not a property any single component can hold. The
 * *fastest* requested interval wins, which makes the outcome independent of
 * mount order — a mount/unmount race can leave the app polling too slowly
 * for an instant, never too fast.
 * ---------------------------------------------------------------------- */

interface Subscriber {
  readonly intervalMs: number
  readonly poll: () => void
}

const subscribers = new Set<Subscriber>()
let timer: ReturnType<typeof setInterval> | null = null
/** The cadence `timer` is running at, so a re-sync that changes nothing
 * leaves the existing interval — and its phase — alone. */
let runningMs: number | null = null

function fastest(): Subscriber | null {
  let best: Subscriber | null = null
  for (const sub of subscribers) {
    if (best === null || sub.intervalMs < best.intervalMs) best = sub
  }
  return best
}

function stop(): void {
  if (timer !== null) clearInterval(timer)
  timer = null
  runningMs = null
}

/** Bring the single interval in line with the current state. */
function sync(): void {
  const winner = fastest()

  // Hidden, or nothing mounted: no interval at all.
  if (winner === null || document.hidden) {
    stop()
    return
  }

  // Already at the right cadence. Restarting here would reset the phase on
  // every unrelated mount, which on a 400ms poll is how a steady interval
  // turns into a burst.
  if (runningMs === winner.intervalMs) return

  const startingFromStopped = timer === null
  stop()
  runningMs = winner.intervalMs
  // The winner is resolved again at fire time rather than captured: the
  // subscriber that set the cadence may have unmounted since.
  timer = setInterval(() => fastest()?.poll(), winner.intervalMs)

  // An immediate read whenever the poll starts from a stop — first mount,
  // and returning from hidden. Deliberately *not* on a cadence change: a
  // route change into Markets already remounts the query observer, which
  // fetches on its own, and polling here as well would double the request.
  if (startingFromStopped) winner.poll()
}

/** Register a subscriber and bring the interval in line with it.
 *
 * **The clamp is the rate limit made structural.** `MARKETS_FOREGROUND_POLL_MS`
 * is a floor *derived from a ceiling*: 400ms is 150/min of Alpaca's hard
 * 200/min `data.` bucket, 300ms is exactly 200/min with nothing left for an
 * on-demand chain, and 200ms is 300/min — over it. Without this line a third
 * call site written as `useMarketPoll(200)` would simply win the registry and
 * nothing would fail, because `ratelimit.py`'s bucket *waits* rather than
 * refusing: the overspend surfaces as latency creep that looks like it
 * worked. The constant test pins the constant; this pins "no subscriber may
 * ask for less than it". Opting out is still `intervalMs <= 0`, and a
 * non-finite interval is refused the same way; neither reaches here. */
function subscribe(requested: Subscriber): () => void {
  const sub: Subscriber = {
    intervalMs: Math.max(requested.intervalMs, MARKETS_FOREGROUND_POLL_MS),
    poll: requested.poll,
  }

  if (subscribers.size === 0) document.addEventListener('visibilitychange', sync)
  subscribers.add(sub)
  sync()

  return () => {
    subscribers.delete(sub)
    if (subscribers.size === 0) document.removeEventListener('visibilitychange', sync)
    sync()
  }
}

/** One poll: read, merge, stamp — and stamp **only** on success.
 *
 * Decision 18's poll writer. The rows go into the one live quote map
 * through `liveFromStockQuote`, which is where the `'poll'` provenance is
 * attached: the server deliberately asserts no `source`, because which
 * endpoint a row arrived on is something this call site knows and a
 * server-side copy of that fact is a copy that can disagree.
 *
 * **`lastPollAt` advances on a successful read and on nothing else**, for
 * the same reason `dataUpdatedAt` does: it answers *"is the poll alive"*,
 * and a failure that stamped would answer it wrongly in the one direction
 * that matters. `refetchStocks` resolves with `null` rather than rejecting,
 * so the failure arrives here as a value instead of as an unhandled
 * rejection every 400ms.
 *
 * **Not `lastTickAt`.** *"Is the stream alive"* and *"is the poll alive"*
 * are different questions with different feeds behind them, and collapsing
 * them would let a healthy poll vouch for a dead socket. */
function pollOnce(client: QueryClient): Promise<void> {
  return refetchStocks(client).then((rows) => {
    if (rows === null) return
    // `request<StockQuote[]>` casts rather than validates, so a 200 whose
    // body is not an array at all arrives here as a `StockQuote[]` that is
    // not one, and the `.map` below would throw inside a 400ms timer --
    // an unhandled rejection two or three times a second in the one
    // console that matters. **That is the whole of what this checks.** A
    // JSON array of non-rows passes it and is merged; the claim is
    // narrowed to the throw deliberately, because a guard credited with
    // validating the shape is one the next reader trusts for more than it
    // does. Our own FastAPI serves this through a Pydantic
    // `response_model`, so a malformed body needs a proxy or a mock.
    if (!Array.isArray(rows)) return
    const store = useUIStore.getState()
    store.applyPolledQuotes(rows.map(liveFromStockQuote))
    store.markPolled(new Date().toISOString())
  })
}

/** Mount the market snapshot poll at `intervalMs`.
 *
 * Mounted twice by design: once app-wide at
 * {@link MARKETS_BACKGROUND_POLL_MS}, and again by `Markets.tsx` at
 * {@link MARKETS_FOREGROUND_POLL_MS} while that page is open. An interval of
 * zero or less mounts nothing, which is how a caller opts out, and so does
 * one that is not a finite number at all; anything
 * faster than {@link MARKETS_FOREGROUND_POLL_MS} is clamped up to it, which
 * is how the vendor bucket stays a property of this module rather than of
 * every call site remembering. */
export function useMarketPoll(intervalMs: number): void {
  const client = useQueryClient()

  useEffect(() => {
    // `Number.isFinite` first, because the clamp below cannot catch what it
    // cannot compare. `NaN <= 0` is false and `Math.max(NaN, 400)` is NaN,
    // and `setInterval(fn, NaN)` runs at 0 — an unthrottled poll against a
    // bucket that waits rather than refusing. `Infinity` is the same hazard
    // from the other end: a delay past 2^31-1 ms overflows and fires almost
    // at once. Neither is reachable while both call sites pass module
    // constants; this is for the day an interval comes from config. Opting
    // out rather than clamping to the floor: a value nobody meant is not a
    // request to poll as fast as the budget allows, and a quote that stops
    // moving is already said on the page by the stale pill.
    if (!Number.isFinite(intervalMs) || intervalMs <= 0) return
    return subscribe({ intervalMs, poll: () => void pollOnce(client) })
  }, [client, intervalMs])
}
