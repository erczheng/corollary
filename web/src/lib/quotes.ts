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
 *    backwards in time.
 * 2. **Price is last-observation-wins on `at`**, with the stream winning a
 *    tie. A poll response older than the entry already held is *discarded
 *    for price and applied for everything else*.
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
 * | `incoming.at` vs `existing.at` | incoming | existing | price |
 * |---|---|---|---|
 * | newer | either   | either   | **incoming** |
 * | older | either   | either   | **existing** — other fields still applied |
 * | equal | `stream` | `poll`   | **incoming** — the stream wins a tie (rule 2) |
 * | equal | `stream` | `stream` | **incoming** — same writer, later read |
 * | equal | `poll`   | `stream` | **existing** — the stream wins a tie (rule 2) |
 * | equal | `poll`   | `poll`   | **incoming** — same writer, later read |
 *
 * Which collapses to: on an equal stamp the incoming price wins unless it is
 * a poll arriving over a streamed entry. */
function replacesPrice(existing: LiveQuote, incoming: LiveQuote): boolean {
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
 * - The poll-only fields are taken from `incoming` whenever `incoming` is a
 *   poll — **including its nulls**, which are the poll's authoritative
 *   "unavailable" and not an absence of opinion — and left untouched
 *   whenever it is the stream, which never carries them.
 * - A *stale* poll still applies its own fields. It lost the price race; it
 *   did not lose the market cap. */
export function mergeQuote(existing: LiveQuote | undefined, incoming: LiveQuote): LiveQuote {
  if (existing === undefined) return incoming

  const takePrice = replacesPrice(existing, incoming)
  const fromPoll = incoming.source === 'poll'

  return {
    symbol: existing.symbol,
    price: takePrice ? incoming.price : existing.price,
    at: takePrice ? incoming.at : existing.at,
    source: takePrice ? incoming.source : existing.source,
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
 * is not a statement that every other symbol stopped existing. */
export function mergeQuotes(
  map: Readonly<Record<string, LiveQuote>>,
  incoming: readonly LiveQuote[],
): Record<string, LiveQuote> {
  if (incoming.length === 0) return map as Record<string, LiveQuote>

  const next: Record<string, LiveQuote> = { ...map }
  for (const quote of incoming) {
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
 * route does carry `previousClose` outright. It carries none of the volume
 * columns, so those arrive null and the field-level merge means a row that
 * `/stocks` has already filled keeps them. */
export function liveFromUnderlyingQuote(row: UnderlyingQuote): LiveQuote {
  return {
    symbol: row.symbol,
    price: row.price,
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
 * **The producer is the browser websocket client, which is not written
 * yet.** `corollary/api/routes/ws.py` and `fanout.py` landed the server half
 * and nothing under `web/`, so this writer has no caller in the app today
 * and is tested directly instead. It exists now rather than later because
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
