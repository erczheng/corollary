/** The one live quote map's merge — decision 18.
 *
 * Pure, no React, tested without rendering anything, the way `orders.ts`,
 * `markets.ts` and `settings.ts` are. The store calls this; the store does
 * not contain it.
 *
 * **Why a merge exists at all.** CLAUDE.md keeps one quote map because two
 * maps are how the Markets table and an Activity row end up disagreeing
 * about AAPL. Phase 2 adds a second *writer* to that one map — the 400ms
 * `/api/markets/stocks` poll and the websocket push — which is the same
 * hazard one level down. Four rules, from decision 18:
 *
 * 1. **An entry carries its provenance**: `at`, the vendor's observation
 *    timestamp, and `source`. Without them two writers cannot tell a newer
 *    price from an older one, the last arrival wins, and the screen flickers
 *    backwards in time. `mergeQuotes` enforces this rather than trusting it:
 *    an observation it cannot order does not become an entry.
 * 2. **Price is last-observation-wins on `at`**, with the stream winning a
 *    tie. A poll response older than the entry already held is *discarded
 *    for price and applied for everything else* — but only while the entry
 *    it lost to is still young, because an entry that has been held longer
 *    than {@link MAX_HELD_AGE_MS} stops winning on its stamp alone. See
 *    {@link replacesPrice}.
 * 3. **The merge is field-level.** The stream carries price and nothing
 *    else; `previousClose`, session volume, average volume and market cap
 *    arrive only on the poll. A stream write must never blank them and a
 *    poll write must never stomp a fresher streamed price. One writer per
 *    field, except price, where rule 2 decides.
 * 4. **Everything derived is derived at read time.** `changeOf` and
 *    `changePctOf` are selectors; nothing here stores a change. Two writers
 *    storing a price and a change independently is how a row reports +1.2%
 *    beside a price that is down.
 */

import type { LiveQuote, QuoteSource, StockQuote, UnderlyingQuote } from './types'

/** A {@link LiveQuote} as the map *holds* it, which is not the same thing as
 * a producer's observation of it.
 *
 * `LiveQuote` describes something a vendor observed: a price, the vendor's
 * stamp for it, and which writer carried it. {@link heldSinceLocalMs}
 * describes something *this map* did — when it adopted that observation —
 * and it is bookkeeping, not evidence about the market. The split is what
 * keeps a producer from forging it: {@link liveFromStockQuote},
 * {@link liveFromUnderlyingQuote} and {@link streamedQuote} all return
 * `LiveQuote` and have no way to write this field. Only {@link mergeQuote}
 * widens an observation into an entry.
 *
 * **Optional rather than required, for one structural reason.** The store's
 * slice is typed `Record<string, LiveQuote>` and hands that map straight
 * back to {@link mergeQuotes}; a required field would not typecheck there
 * without spreading this split through `store.ts` and every reader of the
 * slice, which buys nothing — readers must not see this field, and with the
 * slice typed `LiveQuote` they cannot. The absence has a meaning the code
 * uses rather than tolerates: *not held by this map yet*, which is an age of
 * zero. See {@link heldPastAgeOut}. */
export interface HeldQuote extends LiveQuote {
  /** When this entry was adopted, as a local `Date.now()` in milliseconds.
   *
   * **Not a stamp, and named so it cannot be read as one.** It orders no
   * observation against any other, it is never rendered, and it never goes
   * on the wire; the only thing that reads it is {@link heldPastAgeOut},
   * which asks how long the entry has been held. `at` remains the one
   * observation time and stays the vendor's. */
  readonly heldSinceLocalMs?: number
}

/** An `at` that cannot be ordered against anything.
 *
 * `-Infinity` rather than a throw, and the consequence is deliberate: an
 * unparseable stamp never beats a parseable one for the price, and two
 * unparseable ones fall through to the later-read rule. A synthesised or
 * unreadable observation time must never be allowed to win the merge
 * forever, which is the same reason the server serves *no row* rather than
 * `datetime.now()` for a snapshot it could not stamp. */
function instant(at: string): number {
  const ms = Date.parse(at)
  return Number.isNaN(ms) ? -Infinity : ms
}

/** Can this observation be ranked against another one at all?
 *
 * **Why there is no `?? null` on `at`, three lines from a `previousClose`
 * that has one.** `previousClose` has a canonical absence: `null` is the
 * poll's own "no basis to measure a move from", every selector is written
 * for it, and the merge can carry it around like any other value. `at` has
 * no such value. It is the *vendor's* observation time, so nothing on this
 * side of the wire may stand in for it — not `Date.now()`, not the server's
 * receive time, not an empty string dressed as a stamp. What has to be
 * answered for a missing stamp is therefore the **entry**, not the field,
 * and {@link mergeQuotes} is where that happens.
 *
 * Reachable, not theoretical: `request<StockQuote[]>` casts rather than
 * validating, so a server that omits the key delivers `undefined` as a
 * value the types say cannot exist. Same hazard, same reasoning as the
 * `?? null` beside it in {@link liveFromStockQuote}.
 *
 * **Exported so the socket can ask the same question the merge asks.**
 * `liveSocket.storeHandlers().onQuote` gates its liveness stamp on this
 * (WEB-12): a frame the map will not hold must not make the pill read
 * `Live`. One predicate rather than a second spelling of it in the socket,
 * because the two answers have to agree — a stamp for a frame the merge
 * discarded is precisely the *Live over a frozen timestamp* this codebase
 * says is worse than no badge. */
export function orderable(at: string): boolean {
  return instant(at) !== -Infinity
}

/** How long a held observation may go on winning the merge on its stamp
 * alone.
 *
 * **15 seconds, and the number is borrowed rather than invented.** CLAUDE.md
 * already states one notion of stale — *"past ~15s without a price the status
 * pill reads `stale`"* — and `LiveStatus.STALE_AFTER_MS` is that figure. The
 * app should have one idea of how long a price stays believable, not two.
 *
 * **Its margin over the poll interval is not what keeps it from firing, and
 * reasoning that way will mis-tune it.** `heldSinceLocalMs` is refreshed only
 * on a *winning* write (see `mergeQuote`), so polling 37 times inside the
 * window refreshes nothing in precisely the case this branch exists for — a
 * poll that keeps losing. What actually keeps a healthy tab away from this
 * branch is that every winning write re-stamps the hold, and in the healthy
 * case each poll wins on its own tie. Someone raising
 * `MARKETS_FOREGROUND_POLL_MS` should not conclude from a surviving ratio
 * that the margin is intact: the ratio was never the governing quantity.
 *
 * **The same number as the pill, a different quantity, and pinned together
 * by a test rather than by the code.** Two constants is the right shape:
 * `LiveStatus.STALE_AFTER_MS` answers *"has a frame arrived recently"*, this
 * answers *"has this entry been held too long to keep winning on its
 * stamp"*, and they may legitimately diverge. But nothing in production
 * couples them, so changing one would leave the other behind with the
 * paragraph above still claiming the app has one idea of how long a price
 * stays believable. `quotes.test.ts` therefore asserts
 * `MAX_HELD_AGE_MS === STALE_AFTER_MS`, the same mirror-pin `markets.test.ts`
 * puts on `MARKETS_VIEWPORT_DEBOUNCE_MS`. Grep either name to find both, and
 * move them together — or change the test and this docstring in the same
 * commit that parts them.
 *
 * **Both quantities are local-against-local, which is what makes either one
 * trustworthy.** The pill compares the local clock against `lastTickAt`,
 * which `store.markStreamed` wrote from that same clock. This compares the
 * local clock against the moment the merge adopted the entry. Neither is the
 * age of the vendor's *observation*, which is not measurable on this side of
 * the wire at all — its two ends come from two different clocks. See
 * {@link heldPastAgeOut}, where an earlier version of this file tried.
 *
 * **What this map holds, stated as what is actually true of it.** Its only
 * reader is {@link liveStockRows}, which resolves equity rows — that half
 * was checked. The *writer* side is not filtered: `storeHandlers().onQuote`
 * routes every priced quote frame into `applyStreamedQuote`, and the
 * server's fan-out is sized for 30 equity symbols plus 200 option quotes, so
 * an OCC-symbol entry can sit in this map. It sits there inertly, because
 * nothing reads the non-equity entries. Which feed wrote it no longer bears
 * on this threshold either way: a held-age is the same measurement whatever
 * the vendor's delay, so a 15-minute-delayed options quote is held and
 * released on exactly the terms an IEX one is. The delay shows up in the
 * price — the vendor's business — and not in the merge. */
export const MAX_HELD_AGE_MS = 15_000

/** Has this entry been *held* for longer than {@link MAX_HELD_AGE_MS}?
 *
 * **A held-age, not the age of the vendor's observation, and the difference
 * is the whole of this function.** {@link HeldQuote.heldSinceLocalMs} and
 * `Date.now()` are two reads of the *same* clock, so whatever that clock is
 * doing cancels exactly: a machine five minutes slow, an hour fast, or
 * restored from a snapshot onto a wrong date still releases a held entry
 * after fifteen seconds of elapsed time. `Date.now() - Date.parse(at)`, which
 * this was first written as, subtracts a *remote* stamp from a local clock,
 * and that does not cancel in either direction:
 *
 * - a local clock slow by Δ fires the branch only once the true age passes
 *   `15s + Δ`, so a resumed laptop or an unsynced VM holds the entry for
 *   minutes, and at Δ of hours — a snapshot restore, a clock set back — the
 *   branch never fires all session and the whole fix is a no-op. Nothing
 *   checks for this and it produces no signal;
 * - a local clock fast by Δ fires it on *every* call, so `replacesPrice`
 *   returns true unconditionally and rule 2 is switched off entirely rather
 *   than merely losing its tie-breaks. With one writer, last-write-wins was
 *   monotone in observation order; with two it is not, and an *older*
 *   observation overwriting a newer one is precisely the flicker rules 1 and
 *   2 exist to prevent. The first version of this docstring analysed only
 *   this direction and called it "the pre-stream behaviour". It is not.
 *
 * The same asymmetry is why `LiveStatus.streamState` is trustworthy and this
 * would not have been: the pill compares `now` against `lastTickAt`, which
 * `storeHandlers().onQuote` wrote with `new Date().toISOString()`, so both
 * ends are the same clock and skew cancels. This is the same shape for a
 * different consumer, which is the point rather than a coincidence.
 *
 * **This is the only read of the local clock in this file, and it is not the
 * thing the file forbids.** What is forbidden is a fabricated *observation
 * stamp*: `Date.now()` must never become an `at`, because a synthesised time
 * is newer than every real one by construction and would win the merge
 * forever. That is the server's rule too (`api/routes/markets.py`: never a
 * clock on this side of the wire), and it is why {@link orderable} refuses an
 * entry rather than defaulting its stamp. A held-since marker is none of
 * that: it never orders one observation against another, it is never
 * rendered, and it never goes on the wire. There is precedent in this repo
 * and it is worth naming — `store.markStreamed`'s docstring is explicit that
 * its `at` is *when the update arrived, not the vendor's observation time*,
 * because the Basic plan's `indicative` options feed is 15 minutes delayed
 * and a pill fed the vendor stamp would read `stale` forever on a healthy
 * socket. Same reasoning, different consumer. The line to hold, stated once
 * so the next reader does not have to infer it:
 *
 * - the local clock may decide **whether a held entry still wins**;
 * - the local clock may **never be written into `at`**.
 *
 * **An absent `heldSinceLocalMs` is an age of zero, not an infinite one.**
 * Only {@link mergeQuote} writes the field, so absence means the value was
 * never adopted by this map: a freshly-produced observation handed straight
 * to {@link mergeQuote} by a caller that is not the map. Nothing has held it
 * for any length of time, so it cannot have been held too long.
 *
 * Two entries that behaved badly under the observation-age version and do
 * not under this one. A **bar-stamped** entry, whose `at` is hours old the
 * moment it lands, aged out on contact — which quietly degraded that symbol
 * to last-arrival-wins with no ordering at all; here it is ordered normally
 * for fifteen seconds and then released. A **far-future** stamp
 * (`9999-12-31`, a vendor bug or a bad parse) produced a large *negative*
 * age, so it never aged out and pinned the entry permanently — `Date.parse`
 * returns no `+Infinity`, so nothing else bounded it; here it is released
 * like anything else. That second one is the same arithmetic as the slow
 * clock above, reached from the other end.
 *
 * **Why the branch has to actually fire, post-WEB-2.** Before the eviction
 * gate, a malformed stream frame deleted the entry and the row fell back to
 * the query row's fresh price, which was self-correcting. It no longer does:
 * the entry survives a bad frame, and this age-out is the only remaining
 * bound on it. A thin name, a bar-fallback snapshot, one good streamed
 * quote, then malformed frames and a slow clock was a frozen row with the
 * rescue path removed. */
function heldPastAgeOut(existing: HeldQuote): boolean {
  const heldSince = existing.heldSinceLocalMs
  // Never held by this map, so never held too long. See above.
  if (heldSince === undefined) return false
  return Date.now() - heldSince > MAX_HELD_AGE_MS
}

/** Does `incoming` replace the price, the stamp and the source of
 * `existing`?
 *
 * ## The tie table
 *
 * An equal `at` is reachable, not theoretical: a daily bar's `at` is the
 * interval's *opening* time and does not advance as the close moves, so two
 * consecutive polls of a bar-only snapshot can carry different prices under
 * an identical stamp. Written as a strict `incoming.at > existing.at` this
 * would keep the first price for the rest of the session while the
 * `Read HH:MM:SS ET` header claims the row is current.
 *
 * | condition | incoming | existing | price |
 * |---|---|---|---|
 * | `existing` held (see {@link heldPastAgeOut}) > {@link MAX_HELD_AGE_MS} | either | either | **incoming** — whatever the two stamps say |
 * | `at` newer | either   | either   | **incoming** |
 * | `at` older | either   | either   | **existing** — other fields still applied |
 * | `at` equal | `stream` | `poll`   | **incoming** — the stream wins a tie (rule 2) |
 * | `at` equal | `stream` | `stream` | **incoming** — same writer, later read |
 * | `at` equal | `poll`   | `stream` | **existing** — the stream wins a tie (rule 2) |
 * | `at` equal | `poll`   | `poll`   | **incoming** — same writer, later read |
 *
 * The age-out is checked **first** and overrides the rest — and only ever in
 * the direction of letting `incoming` win, so until it fires the ordinary
 * stamp comparison still runs and a real streamed push still beats a held
 * bar-stamped entry on the stamps. It can hand the entry only to an
 * observation the map could hold: an `incoming` whose `at` is absent or
 * unparseable does not win it (WEB-10, at the branch). The rows below it
 * collapse to: on an equal stamp the incoming price wins unless it is a poll
 * arriving over a streamed entry.
 *
 * ## Why the age-out row exists
 *
 * The stamp rows alone freeze a row for a session, and the sequence is
 * ordinary rather than exotic. The defect class is **a snapshot stamp that
 * is fixed for the session**, and `markets._spot` has two rungs that produce
 * one. Its order is quote mid, then the last print, then the daily close: the
 * bar rung needs no quote mid *and* no `latest_trade`, and `Bar.at` is the
 * interval's *opening* timestamp — Alpaca's convention, which the
 * no-look-ahead rule depends on. The trade rung freezes identically whenever
 * that print is from a prior session, which is the ordinary state of a thin
 * name with no IEX print today. Naming only the bar rung here would send the
 * next reader looking for the wrong trigger; the age-out fixes both the same
 * way. So on such a name the poll's stamp is **fixed for the session while
 * its price advances**. Once the stream has pushed one two-sided quote for that symbol,
 * every later poll is stamped earlier than the held stream entry, loses the
 * price race, and loses it again on every poll for the rest of the day. The
 * row then shows a price nobody has observed since the morning, and it is
 * that price that drives {@link changeOf}'s day change and its
 * bullish/bearish colour, the gainers/losers ranking, and
 * `ChainOrderTicket`'s moneyness sentence — under the word "risk". Nothing on
 * screen contradicts it: `LiveStockRow.source` and the per-row `at` are
 * computed and never rendered, and the global `Read HH:MM:SS ET` header
 * advances on every successful fetch.
 *
 * The age-out was chosen over the two alternatives deliberately. A
 * server-side refusal to serve bar-stamped rows drops thinly-traded names off
 * the Markets table entirely; rendering the staleness per row labels the
 * problem but leaves the wrong price driving the change, the ranking and the
 * moneyness sentence, with fresh polls still discarded. What this buys: no
 * row vanishes, no timestamp is fabricated, and the row recovers on the first
 * poll after {@link MAX_HELD_AGE_MS} — a bounded ~15s of staleness instead of
 * a session of it.
 *
 * ## What it costs when the stream is *not* quiet
 *
 * "A bounded ~15s of staleness" is the quiet-stream case, and saying only
 * that would be a half-answer. Take a symbol whose REST snapshot is bar-only
 * (stamp `B`, hours old) which the socket *also* pushes for, at a cadence `C`
 * longer than {@link MAX_HELD_AGE_MS}. The merge then does not converge — it
 * alternates: the pushed quote mid for 15s, the bar's last-trade close for
 * the remaining `C − 15`s, indefinitely. On a thin name those two can differ
 * by the spread, so the day change, its bullish/bearish colour, the
 * gainers/losers rank and `ChainOrderTicket`'s moneyness sentence flip on
 * that period, and a contract can be watched going ITM → OTM → ITM with
 * nothing touched.
 *
 * That is an accepted consequence of the ruling rather than an open defect,
 * and it is recorded here so the next reader meets it in a docstring instead
 * of on a screen. Both alternating figures are real, recently-observed prices
 * of the same symbol; what this replaced showed one price nobody had observed
 * since the morning, for the rest of the session, with nothing on screen
 * saying so. */
function replacesPrice(existing: HeldQuote, incoming: LiveQuote): boolean {
  // First, and ahead of both stamps: a held entry that has gone quiet stops
  // being evidence about the present. See `heldPastAgeOut` for why reading
  // the local clock here is not the thing `orderable` forbids.
  //
  // The `orderable` conjunct is WEB-10, and it is what keeps *nothing
  // unorderable enters the map* true of this function rather than only of
  // {@link mergeQuotes}' pre-filter. Without it the age-out returned `true`
  // before `incoming.at` was looked at, so an unstampable observation won
  // against an aged-out entry and `mergeQuote` wrote `at: undefined` — an
  // entry the merge could never order again. Falling through instead costs
  // nothing: `instant` sends an unorderable stamp to `-Infinity`, so the
  // comparison below keeps `existing` until an observation the map *can*
  // hold arrives, exactly as an ignored stream frame does.
  if (heldPastAgeOut(existing) && orderable(incoming.at)) return true

  const a = instant(incoming.at)
  const b = instant(existing.at)

  if (a > b) return true
  if (a < b) return false
  return !(incoming.source === 'poll' && existing.source === 'stream')
}

/** Merge one observation into the entry already held.
 *
 * `existing` is `undefined` for a symbol the map has never seen, which is
 * every symbol on a cold start — the live map is empty until a writer fills
 * it, so that a never-polled symbol has *no* live quote rather than a stale
 * or invented one.
 *
 * Field-level, per rule 3:
 *
 * - `price`, `at` and `source` move as one, because they describe a single
 *   observation. Keeping `at` from a losing write would stamp a price that
 *   is not the one on screen.
 * - `heldSinceLocalMs` moves **with** them, because it dates that
 *   observation's adoption and nothing else. When the incoming write loses
 *   the price race the held observation is unchanged, so its held-since must
 *   not be refreshed — refreshing it would let a 400ms poll renew the hold
 *   on every cycle and the age-out would never fire at all. This function
 *   and its `existing === undefined` base case are the only two places the
 *   field is written: the three producers describe *observations*, and only
 *   the merge decides what the map *holds*.
 * - The base case therefore copies `incoming` rather than returning it
 *   as-is: an entry carries one field that an observation does not.
 * - The poll-only fields are taken from `incoming` whenever `incoming` is a
 *   poll — **including its nulls**, which are the poll's authoritative
 *   "unavailable" and not an absence of opinion — and left untouched
 *   whenever it is the stream, which never carries them.
 * - A *stale* poll still applies its own fields. It lost the price race; it
 *   did not lose the market cap. */
export function mergeQuote(existing: HeldQuote | undefined, incoming: LiveQuote): HeldQuote {
  if (existing === undefined) return { ...incoming, heldSinceLocalMs: Date.now() }

  const takePrice = replacesPrice(existing, incoming)
  const fromPoll = incoming.source === 'poll'

  return {
    symbol: existing.symbol,
    price: takePrice ? incoming.price : existing.price,
    at: takePrice ? incoming.at : existing.at,
    source: takePrice ? incoming.source : existing.source,
    // With the observation it describes: now for an adopted one, untouched
    // for a held one. `?? Date.now()` covers an `existing` that never came
    // from this map, which only a direct caller can produce.
    heldSinceLocalMs: takePrice ? Date.now() : (existing.heldSinceLocalMs ?? Date.now()),
    previousClose: fromPoll ? incoming.previousClose : existing.previousClose,
    volume: fromPoll ? incoming.volume : existing.volume,
    volumeSession: fromPoll ? incoming.volumeSession : existing.volumeSession,
    volumeDate: fromPoll ? incoming.volumeDate : existing.volumeDate,
    avgVolume: fromPoll ? incoming.avgVolume : existing.avgVolume,
    marketCap: fromPoll ? incoming.marketCap : existing.marketCap,
  }
}

/** Merge a batch into a map, returning a new map.
 *
 * A new object rather than a mutation, because this feeds a Zustand slice
 * and a mutated map is a map React cannot see change. Symbols the batch does
 * not mention are carried through untouched: a poll for one page's symbols
 * is not a statement that every other symbol stopped existing.
 *
 * ## The map holds only entries it can order
 *
 * This is the one way into the map — both writers reach it through
 * `store.applyPolledQuotes` and `store.applyStreamedQuote` — so it is where
 * rule 1, *an entry carries its provenance*, is made structurally true
 * rather than assumed. An observation whose `at` is absent or unparseable
 * (see {@link orderable}) never becomes an entry. What happens to the entry
 * already held depends on **which writer** sent the unorderable
 * observation, and the asymmetry is the whole of WEB-2:
 *
 * - **From the poll it removes the entry.** The discarded observation and
 *   the row the table falls back to came from the *same response*, so
 *   dropping the entry costs nothing and fixes everything — see below.
 * - **From the stream it is ignored.** The entry it would delete was
 *   written by a different, healthy writer, and the stream carries nothing
 *   that could replace it. A malformed frame is evidence about the frame,
 *   not about the poll's price. Deleting on it took a good price off the
 *   screen on the strength of a bad one, while `markStreamed` stamped
 *   `lastTickAt` anyway so the pill went on reading `Live`, with nothing
 *   saying the row had just lost its live entry.
 *
 * That last clause covers **one** case and means only that one: a frame
 * carrying a valid mid whose `at` is absent or unparseable. A frame that
 * prices nothing never reaches `markStreamed` at all — `liveSocket` returns
 * early on a null mid — so the pill never claims a price arrived when none
 * did. **Nor does this one, as of WEB-12:** `storeHandlers().onQuote` now
 * gates the liveness stamp on {@link orderable} too, so the frame that is
 * ignored here stamps nothing either. An earlier revision argued the other
 * way — that `lastTickAt` answers *"did a frame arrive"* and one had, with
 * a price — and it was defensible only while nothing rendered `lastTickAt`.
 * The day a stream pill returns to Markets or Activity it would read `Live`
 * over a map that took nothing from the frame, which is the same *badge
 * over a frozen timestamp* the delete was reverted for.
 *
 * Ignoring cannot freeze a row the way an ignored *poll* would. The held
 * entry stays only until the next orderable observation, the poll produces
 * one every cycle, and {@link MAX_HELD_AGE_MS} bounds the wait even if the
 * poll goes quiet.
 *
 * **Why an unstamped poll cannot just lose the price race.** `-Infinity`
 * loses to every real stamp, and keeps losing. One stamped write — a single
 * pushed quote — would pin the entry, and every later unstamped poll would be discarded
 * for price for the rest of the session, while `Markets.tsx`'s `Read
 * HH:MM:SS ET` header, which advances on any successful fetch, went on
 * saying the row was current. A quiet stream is not even a fault on this
 * socket: the viewport hint stops pushes for a symbol the moment it scrolls
 * off screen, so *last push, then only polls* is the ordinary case rather
 * than the broken one.
 *
 * **Why that mix is reachable.** `dc09147` shipped `/api/ws` with
 * `WsQuote.at` already required; `86a239f`, two commits later, is what put
 * `at` on the REST rows. A browser at HEAD against a server anywhere in
 * that window gets stamped pushes and unstamped polls at the same time.
 *
 * **Why dropping the entry costs nothing.** The map has exactly one reader,
 * {@link liveStockRows}, and a symbol with no entry there renders the query
 * row's own price — from the very response the unstamped observation
 * arrived in, with that row's volume and basis beside it. No entry is not a
 * blank row; it is the server's own figure at the poll's cadence. The cost
 * runs the other way and is the lesser one: against such a server a live
 * push and the poll alternate, sub-second apart, both of them real prices
 * of the same symbol, where rule 1's flicker is an *older* price
 * overwriting a newer one.
 *
 * The *entry-level* decision is this function's alone: {@link mergeQuote}
 * answers the field-level question — what does the entry *become* — and an
 * unorderable observation can only reach it from a caller that is not the
 * map. It is no longer defenceless when one does, though. WEB-10 put an
 * {@link orderable} guard on the age-out branch of `replacesPrice` — the
 * branch that could adopt such an observation *over a perfectly good held
 * entry* — so a direct caller now gets the held entry back rather than one
 * stamped `undefined`.
 *
 * **That is a guard on the branch, not a proof about the function**, and
 * the difference is worth stating so a later reader does not take it for
 * one. One route still adopts an unorderable `at`: the equal-stamp tie
 * where *both* sides are unorderable, `instant` sending each to
 * `-Infinity`, which {@link instant} already documents as deliberate
 * ("two unparseable ones fall through to the later-read rule"). It needs
 * an `existing` that is itself unorderable, and this filter is what makes
 * that unreachable — no map entry can ever be in that state, and
 * `mergeQuotes` is `mergeQuote`'s only production caller. So the invariant
 * is enforced in two places for two different readers: here for the map,
 * and on the age-out branch for anyone reading the exported function on
 * its own. */
export function mergeQuotes(
  map: Readonly<Record<string, HeldQuote>>,
  incoming: readonly LiveQuote[],
): Record<string, HeldQuote> {
  if (incoming.length === 0) return map as Record<string, HeldQuote>

  const next: Record<string, HeldQuote> = { ...map }
  for (const quote of incoming) {
    if (!orderable(quote.at)) {
      // The delete is the poll's, and only the poll's (WEB-2). A poll's
      // fallback row is in the same response; a stream frame has no such
      // row behind it, and the entry it would evict belongs to the other
      // writer. Either way nothing unorderable enters the map.
      if (quote.source === 'poll') delete next[quote.symbol]
      continue
    }
    next[quote.symbol] = mergeQuote(next[quote.symbol], quote)
  }
  return next
}

/* -------------------------------------------------------------------------
 * The two producers
 * ---------------------------------------------------------------------- */

/** A polled `/api/markets/stocks` row as a live entry.
 *
 * **A straight read of the basis, never a subtraction.** Rule 4 needs the
 * previous close rather than the change: a streamed price arriving a moment
 * later has to be measurable against *something*, and a change the server
 * computed is a fact about the poll's price, not about the stream's.
 *
 * This read replaces `price - change`, which was exact in the server's
 * `Decimal`s and lossy on the wire — every money field serializes to an
 * independent IEEE double, so the subtraction was a third rounding over two
 * rounded inputs and moved the rendered cent whenever the true change landed
 * on a half-cent. `_spot` prefers the quote mid, so a penny-wide quote puts
 * it there routinely; at the limit it printed a bullish-green `+$0.00` for a
 * move that happened. `previous_close` is now on the wire instead. */
export function liveFromStockQuote(row: StockQuote): LiveQuote {
  return {
    symbol: row.symbol,
    price: row.price,
    // Bare, and deliberately bare one line above a `?? null`: there is no
    // value this side of the wire may put in a vendor observation time, so
    // an absent key stays absent here and is refused entry to the map by
    // `mergeQuotes`, which is the only place it can be answered without
    // inventing a stamp. See `orderable`.
    at: row.at,
    source: 'poll',
    // `?? null`, not a bare read: nullable *and* absent are both reachable
    // here, the same distinction `markets.ts#latestVolumeDate` spells out.
    // An older server omits the key entirely and `request<StockQuote[]>`
    // casts rather than validating, so `undefined` arrives as a value the
    // types say cannot exist. `changeOf` would survive it -- its guard is
    // `== null`, which catches both -- so this is not rescuing the
    // selector; it is keeping one canonical absence in the map, because
    // `mergeQuote` compares these fields and `undefined` vs `null` is a
    // difference the merge would have to know about and does not.
    previousClose: row.previousClose ?? null,
    volume: row.volume,
    volumeSession: row.volumeSession,
    volumeDate: row.volumeDate,
    avgVolume: row.avgVolume,
    marketCap: row.marketCap,
  }
}

/** A polled `/api/markets/underlyings` row as a live entry.
 *
 * The chart path writes the same map — one symbol has one price — and this
 * route does carry `previousClose` outright.
 *
 * **It carries none of the volume columns, and the merge does *not* protect
 * them.** `mergeQuote` keys the column fields on `source === 'poll'`, which
 * is true of this entry, so wiring this function into `applyPolledQuotes`
 * would overwrite `volume`, `volumeSession`, `volumeDate`, `avgVolume` and
 * `marketCap` with null on every symbol the chart path touches. A null
 * `marketCap` then sorts *last in both directions* (`markets.ts`), so SPY
 * would fall to the bottom of a descending market-cap sort behind a blank
 * cell, with nothing on screen saying why. Unreachable today — this function
 * has no production caller and `useMarketPoll` writes `liveFromStockQuote` —
 * and it must not be wired up until the merge distinguishes "this writer has
 * no opinion on that column" from "that column is null". */
export function liveFromUnderlyingQuote(row: UnderlyingQuote): LiveQuote {
  return {
    symbol: row.symbol,
    price: row.price,
    // Bare for the reason `liveFromStockQuote` states: a missing vendor
    // stamp is refused an entry by `mergeQuotes`, never defaulted here.
    at: row.at,
    source: 'poll',
    previousClose: row.previousClose,
    volume: null,
    volumeSession: null,
    volumeDate: null,
    avgVolume: null,
    marketCap: null,
  }
}

/** A pushed quote as a live entry.
 *
 * **The producer is the browser websocket client, and it now exists** —
 * `web/src/lib/liveSocket.ts`, whose `storeHandlers().onQuote` routes a
 * `quote` frame here through `store.applyStreamedQuote`. **Where that
 * client is mounted is deliberately not stated here**, and the omission is
 * the point: this docstring is about the merge, and a sentence about the
 * mount is a sentence that goes stale somewhere else. The one that used to
 * sit here said nothing called `startLiveSocket` yet, and it was false the
 * moment something did. `startLiveSocket`'s own docstring names its caller.
 * Either way the merge is tested directly and against a fake socket, and it
 * exists now rather than later because
 * the *merge* is what has to be in place before the second writer arrives —
 * landing the rules afterwards means shipping the flickers-backwards-in-time
 * bug first and fixing it second.
 *
 * Price and stamp only: every other field on a `LiveQuote` belongs to the
 * poll, and a stream write must never blank them. The nulls here are what a
 * first-ever write for an unpolled symbol leaves behind; `mergeQuote` never
 * copies them over an existing entry. */
export function streamedQuote(symbol: string, price: number, at: string): LiveQuote {
  return {
    symbol,
    price,
    at,
    source: 'stream',
    previousClose: null,
    volume: null,
    volumeSession: null,
    volumeDate: null,
    avgVolume: null,
    marketCap: null,
  }
}

/* -------------------------------------------------------------------------
 * Rule 4 — derived at read time, never stored
 * ---------------------------------------------------------------------- */

/** Anything that carries a price and the close it is measured from.
 *
 * Structural rather than `LiveQuote`, because the same two selectors answer
 * for a `LiveQuote` on the Markets table and an `UnderlyingQuote` on a
 * position row, and the day's move must not be computed two ways in two
 * places. Both guards below are `== null`, not `===`: these fields are
 * nullable by contract and *absent* on any server predating the commit that
 * added them, and `request<T>` casts unvalidated JSON — the strict check
 * lets `undefined` through and renders `$NaN`. Same rule, same reason, as
 * `markets.ts#latestVolumeDate`. */
export interface Measured {
  price: number
  previousClose: number | null
}

/** The grid every money figure on this wire actually lives on.
 *
 * A millionth of a dollar: four orders of magnitude finer than the finest
 * real tick (options under $3 quote in $0.0001) and ten orders coarser than
 * the error a double subtraction introduces at this app's largest figure
 * (~1e-11 at $99,728.08). So it snaps the arithmetic and cannot move a
 * price.
 *
 * `price * 1e6` stays an exact integer well inside 2^53 for anything this
 * app serves, so the multiply-round-divide is itself lossless. */
const MONEY_GRID = 1e6

/** The move in dollars, or null when there is no basis to measure one.
 *
 * **Null, never 0.** No previous close means nobody measured the thing the
 * move would be measured from, and a `+$0.00` in a column of dollars claims
 * the price was unchanged.
 *
 * **Snapped to {@link MONEY_GRID}, and that is not cosmetic.** Money is
 * `Decimal` everywhere the server computes it and an IEEE double everywhere
 * the wire carries it, so this subtraction is exact in the arithmetic the
 * server did and off by a fraction of a ULP in the arithmetic available
 * here — and the error lands on the wrong side of a rounding boundary
 * exactly when the true move is a half-cent. `_spot` prefers the quote mid,
 * so a penny-wide quote puts it there routinely. Measured, on this repo's
 * own fixture: `764.285 - 757.87` is `6.414999999999964`, which renders
 * `+$6.41` where the exact `6.415` renders `+$6.42`. At the limit
 * `100.005 - 100` is `0.004999999999995`, which renders **`+$0.00`** — and
 * `value > 0`, so it renders it in bullish green: a column of dollars
 * stating a move did not happen, which is the one thing the null above
 * exists to prevent. The snap recovers the server's own figure exactly in
 * every case checked, including at $99,728.08.
 *
 * This is display arithmetic and stays display arithmetic; the server's
 * `Decimal` is authoritative and this is the live estimate between
 * refreshes. */
export function changeOf(quote: Measured): number | null {
  if (quote.previousClose == null) return null
  return Math.round((quote.price - quote.previousClose) * MONEY_GRID) / MONEY_GRID
}

/** The move in percent — percent units, matching `formatPct`, so 1.2 is
 * 1.20%.
 *
 * Null wherever {@link changeOf} is, and additionally on a zero previous
 * close, which is a division by zero rather than an infinite rally. The
 * server's `_change_pct` refuses the same two cases. */
export function changePctOf(quote: Measured): number | null {
  if (quote.previousClose == null || quote.previousClose === 0) return null
  // Over the *snapped* move, not a second subtraction: the percent and the
  // dollars beside it have to be the same move, and two independent
  // roundings is how a row shows `+$0.01` at `+0.00%`.
  const move = changeOf(quote)
  if (move === null) return null
  return (move / quote.previousClose) * 100
}

/* -------------------------------------------------------------------------
 * The Markets stock table's rows
 * ---------------------------------------------------------------------- */

/** A stock row as the table renders it: the query's row, with the live
 * price merged in and the change derived from it.
 *
 * `change` and `changePct` exist **only** here. They are not wire fields —
 * `StockQuote` carries `price` and `previousClose` and nothing else about
 * the day — because two writers storing a price and a change independently
 * is how a row reports +1.2% beside a price that is down. This type is the
 * one place the pair is allowed to exist, and it is a read-time derivation
 * every time. */
export type LiveStockRow = StockQuote & {
  change: number | null
  changePct: number | null
  /** Which writer the rendered price came from, or null for a row with no
   * live entry at all — the query's own price, straight through. */
  source: QuoteSource | null
}

/** Resolve one table's worth of rows against the live map.
 *
 * **A symbol with no live entry renders the query row's own value.** Not a
 * fixture, and not a blank: the row arrived from the server with a price on
 * it, and that price is the best thing known about the symbol until a writer
 * says otherwise. The change beside it is still derived rather than read, so
 * the two numbers cannot disagree with each other on any row on the screen.
 * That fallback is also what an *unstamped* observation resolves to:
 * `mergeQuotes` holds no entry for one, so the row shown is the price the
 * server sent, refreshed every poll, rather than a price frozen behind a
 * stamp it could not be ranked against.
 *
 * **The basis falls back to the query row's**, which is what a *stream-first*
 * symbol needs. `streamedQuote` writes `previousClose: null` — the stream
 * carries a price and nothing else — so a symbol the websocket reports
 * before the first poll lands would otherwise show a real streamed price
 * beside an em dash reading "no previous daily bar to measure the move
 * from". That reason is false: the row on screen carries the basis. A blank
 * cell is survivable for one poll interval; a stated reason that is untrue
 * is not.
 *
 * Returns a new array; nothing here sorts or mutates in place. */
export function liveStockRows(
  rows: readonly StockQuote[],
  quotes: Readonly<Record<string, LiveQuote>>,
): LiveStockRow[] {
  return rows.map((row) => {
    const quote = quotes[row.symbol] ?? liveFromStockQuote(row)
    const live = quotes[row.symbol] !== undefined
    // `??`, so a null *or* absent entry basis falls through to the row's.
    const measured: Measured = {
      price: quote.price,
      previousClose: quote.previousClose ?? row.previousClose ?? null,
    }

    return {
      ...row,
      price: quote.price,
      at: quote.at,
      change: changeOf(measured),
      changePct: changePctOf(measured),
      source: live ? quote.source : null,
    }
  })
}
