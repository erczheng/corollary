import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { LiveQuote, StockQuote, UnderlyingQuote } from './types'
// Imported *into the test only*, for the mirror pin below. The two
// constants stay uncoupled in production code on purpose.
import { STALE_AFTER_MS } from '../components/LiveStatus'
import {
  changeOf,
  changePctOf,
  liveFromStockQuote,
  liveFromUnderlyingQuote,
  liveStockRows,
  MAX_HELD_AGE_MS,
  mergeQuote,
  mergeQuotes,
  streamedQuote,
} from './quotes'

const EARLY = '2026-09-16T14:30:00Z'
const LATE = '2026-09-16T14:30:01Z'

/** **No file-scoped clock, and that is a property worth noticing.** The
 * age-out measures how long the *map* has held an entry, not how old the
 * vendor says the observation is, so a dated fixture stamp no longer ages
 * out against the real clock and every stamp-ordering test below is
 * clock-independent. Timer control survives in exactly one block — the
 * age-out's, which has to advance past `MAX_HELD_AGE_MS` on purpose rather
 * than avoid tripping it by accident.
 *
 * Fake timers there, not a `now` parameter threaded through `replacesPrice`
 * → `mergeQuote` → `mergeQuotes`: those signatures are the store's, and a
 * test-only argument on the one public entry point into the live map is a
 * worse trade than a fake clock in the one block that needs it. */
function polled(over: Partial<LiveQuote> = {}): LiveQuote {
  return {
    symbol: 'AAPL',
    price: 100,
    at: EARLY,
    source: 'poll',
    previousClose: 99,
    volume: 1_000_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-16',
    avgVolume: 2_000_000,
    marketCap: 3_000,
    ...over,
  }
}

function streamed(over: Partial<LiveQuote> = {}): LiveQuote {
  return { ...streamedQuote('AAPL', 101, EARLY), ...over }
}

describe('mergeQuote — rule 2, last observation wins on `at`', () => {
  it('takes the price of a strictly newer observation, from either writer', () => {
    const existing = polled({ price: 100, at: EARLY })

    expect(mergeQuote(existing, polled({ price: 105, at: LATE })).price).toBe(105)
    expect(mergeQuote(existing, streamed({ price: 105, at: LATE })).price).toBe(105)
  })

  it('discards a strictly older observation for price, from either writer', () => {
    const existing = polled({ price: 100, at: LATE })

    expect(mergeQuote(existing, polled({ price: 105, at: EARLY })).price).toBe(100)
    expect(mergeQuote(existing, streamed({ price: 105, at: EARLY })).price).toBe(100)
  })

  it('keeps the winning observation’s stamp and source, not the loser’s', () => {
    const stale = mergeQuote(polled({ at: LATE, source: 'stream' }), polled({ at: EARLY }))
    expect(stale.at).toBe(LATE)
    expect(stale.source).toBe('stream')

    const fresh = mergeQuote(polled({ at: EARLY }), streamed({ at: LATE }))
    expect(fresh.at).toBe(LATE)
    expect(fresh.source).toBe('stream')
  })

  it('orders on the instant, not on the string — two spellings of one time tie', () => {
    const existing = polled({ price: 100, at: '2026-09-16T14:30:00Z', source: 'stream' })
    const incoming = polled({ price: 105, at: '2026-09-16T10:30:00.000-04:00' })

    // Equal instant, poll over a streamed entry: the stream holds.
    expect(mergeQuote(existing, incoming).price).toBe(100)
  })
})

/** The tie table, one test per cell. An equal `at` is reachable rather than
 * theoretical: a daily bar's `at` is the interval's *opening* time and does
 * not advance as the close moves, so two consecutive polls of a bar-only
 * snapshot carry different prices under an identical stamp. A strict
 * `incoming.at > existing.at` would freeze the first price for the session
 * while the `Read …` header claims the row is current. */
describe('mergeQuote — the equal-`at` tie table', () => {
  it('stream over poll: the stream wins the tie', () => {
    const merged = mergeQuote(polled({ price: 100, at: EARLY }), streamed({ price: 105, at: EARLY }))
    expect(merged.price).toBe(105)
    expect(merged.source).toBe('stream')
  })

  it('stream over stream: the later read wins', () => {
    const existing = streamed({ price: 100, at: EARLY })
    expect(mergeQuote(existing, streamed({ price: 105, at: EARLY })).price).toBe(105)
  })

  it('poll over stream: the existing streamed price stands', () => {
    const merged = mergeQuote(streamed({ price: 105, at: EARLY }), polled({ price: 100, at: EARLY }))
    expect(merged.price).toBe(105)
    expect(merged.source).toBe('stream')
  })

  it('poll over poll: the later read wins, so a frozen bar stamp still moves', () => {
    const merged = mergeQuote(polled({ price: 100, at: EARLY }), polled({ price: 105, at: EARLY }))
    expect(merged.price).toBe(105)
    expect(merged.source).toBe('poll')
  })
})

/** Two constants, one number, and nothing in production holding them
 * together.
 *
 * `MAX_HELD_AGE_MS` and `LiveStatus.STALE_AFTER_MS` are deliberately
 * separate — one asks *"has a frame arrived recently"*, the other *"has this
 * entry been held too long to keep winning on its stamp"*, and they may
 * legitimately diverge. But `MAX_HELD_AGE_MS`'s docstring claims the app has
 * one idea of how long a price stays believable, so changing one number
 * alone would leave the other behind and the docstring wrong. The pin lives
 * here rather than in a shared constant, the same way `markets.test.ts`
 * mirror-pins `MARKETS_VIEWPORT_DEBOUNCE_MS`. */
describe('MAX_HELD_AGE_MS', () => {
  it('is the same fifteen seconds the status pill calls stale', () => {
    expect(MAX_HELD_AGE_MS).toBe(STALE_AFTER_MS)
  })
})

/** The reported defect, reproduced as reported rather than abstracted.
 *
 * `markets._spot` falls back to `Spot(price=daily.close, at=daily.at)` for a
 * symbol with no two-sided quote, and `Bar.at` is the interval's *opening*
 * timestamp (Alpaca's convention; the no-look-ahead rule depends on it). So
 * on a thin name the poll's stamp is fixed for the session while its price
 * advances. Once the stream has pushed one quote for that symbol, every
 * later poll is stamped earlier than the held entry and — before the age-out
 * — lost the price race on every poll for the rest of the day, with the
 * frozen price driving the day change and its colour, the gainers/losers
 * ranking and `ChainOrderTicket`'s moneyness sentence. */
describe('replacesPrice — the daily-bar fallback must not freeze a row for the session', () => {
  /** 09:30 ET: the opening stamp of today's daily bar, which is what a
   * bar-fallback snapshot reports all session. */
  const BAR_AT = '2026-09-16T13:30:00Z'
  /** A plausible vendor stamp on the one two-sided quote the stream saw. */
  const STREAM_AT = EARLY
  /** The local clock when each of these tests starts: five seconds after the
   * stream's stamp, which is an ordinary amount of latency-plus-skew. The
   * block below is largely about that five seconds making no difference. */
  const ADOPTED_AT = Date.parse(STREAM_AT) + 5_000

  /** The one block in this file that reads a clock, because it is the one
   * that has to advance past `MAX_HELD_AGE_MS` deliberately. Elapsed time is
   * advanced with `advanceTimersByTime`, not jumped to with `setSystemTime`:
   * the hold starts when the *merge* adopts an observation, so what these
   * tests need to say is "fifteen seconds later", never "fifteen seconds
   * after the vendor's stamp". */
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(ADOPTED_AT)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const thinRow: StockQuote = {
    symbol: 'THIN',
    name: 'Thinly Traded Inc.',
    price: 100,
    at: BAR_AT,
    previousClose: 100,
    volume: 4_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-16',
    avgVolume: 5_000,
    marketCap: 400,
  }

  /** One poll of the bar-fallback snapshot: advancing close, static stamp. */
  function barPoll(price: number, at: string = BAR_AT): LiveQuote {
    return liveFromStockQuote({ ...thinRow, price, at })
  }

  it('recovers on the first poll past MAX_HELD_AGE_MS rather than freezing at the pushed price', () => {
    // 1. The stream pushes one two-sided quote, stamped by the vendor.
    let map = mergeQuotes({}, [streamedQuote('THIN', 101, STREAM_AT)])
    expect(map.THIN.price).toBe(101)

    // 2. The snapshot falls back to the daily bar from here on. Inside the
    //    window the push still holds, which is rule 2 working: a genuinely
    //    older observation does not overwrite a fresh one.
    map = mergeQuotes(map, [barPoll(102)])
    expect(map.THIN.price).toBe(101)

    // At the boundary exactly, still held — the comparison is a strict `>`.
    vi.advanceTimersByTime(MAX_HELD_AGE_MS)
    map = mergeQuotes(map, [barPoll(103)])
    expect(map.THIN.price).toBe(101)

    // 3. One millisecond past it, the poll wins. This is the assertion the
    //    pre-fix `replacesPrice` fails: it held 101 here and for every poll
    //    after, for the rest of the session.
    vi.advanceTimersByTime(1)
    map = mergeQuotes(map, [barPoll(104)])
    expect(map.THIN.price).toBe(104)
    expect(map.THIN.source).toBe('poll')

    // 4. And it keeps tracking: the bar stamp now ties with itself, so the
    //    later read wins and the row follows the close for the rest of the
    //    session instead of stopping again at 104.
    map = mergeQuotes(map, [barPoll(105)])
    expect(map.THIN.price).toBe(105)
    map = mergeQuotes(map, [barPoll(106)])
    expect(map.THIN.price).toBe(106)
  })

  it('recovers the derived day change with it, which is what the frozen price corrupted', () => {
    const row = { ...thinRow, price: 104 }

    let map = mergeQuotes({}, [streamedQuote('THIN', 101, STREAM_AT)])
    const frozen = liveStockRows([row], map)[0]
    expect(frozen.price).toBe(101)
    expect(frozen.change).toBe(1)

    vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
    map = mergeQuotes(map, [liveFromStockQuote(row)])
    const recovered = liveStockRows([row], map)[0]

    expect(recovered.price).toBe(104)
    expect(recovered.change).toBe(4)
    expect(recovered.source).toBe('poll')
  })

  it('never writes the local clock into `at` — the winning stamp is the vendor’s, even going backwards', () => {
    let map = mergeQuotes({}, [streamedQuote('THIN', 101, STREAM_AT)])
    vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
    map = mergeQuotes(map, [barPoll(104)])
    const entry = map.THIN

    // The age-out decides *whether* the incoming write wins. It never
    // supplies a stamp: the entry carries the bar's own opening time, which
    // is earlier than the stream stamp it just replaced.
    expect(entry.price).toBe(104)
    expect(entry.at).toBe(BAR_AT)
    expect(Date.parse(entry.at)).toBeLessThan(Date.parse(STREAM_AT))
    expect(Date.parse(entry.at)).not.toBe(Date.now())

    // The clock read lands on the entry's own bookkeeping instead, where it
    // orders no observation against any other and nothing renders it.
    expect(entry.heldSinceLocalMs).toBe(Date.now())
  })

  /** WEB-10. `mergeQuotes` filters unorderable observations out before the
   * merge ever sees one, so the map's own path cannot reach this — these
   * call the **exported** {@link mergeQuote} directly, the way the finding
   * is written and the way a future caller would.
   *
   * The age-out is the one branch that decides the race before either stamp
   * is read, so it was the one branch that could adopt an observation the
   * map can never order again, writing `at: undefined` into an entry. What
   * it must do instead is nothing: hold what it has until an observation
   * arrives that can be held. */
  describe('the age-out hands the entry only to an observation the map can hold', () => {
    /** A held entry, adopted through the merge so it carries a real
     * `heldSinceLocalMs`, then left to age past the window. */
    function agedOut() {
      const entry = mergeQuote(undefined, streamedQuote('THIN', 101, STREAM_AT))
      vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
      return entry
    }

    const NO_STAMP = undefined as unknown as string

    it.each([
      ['a stream frame', (): LiveQuote => ({ ...streamedQuote('THIN', 999, STREAM_AT), at: NO_STAMP })],
      ['a poll row', (): LiveQuote => liveFromStockQuote({ ...thinRow, price: 999, at: NO_STAMP })],
    ])('refuses an unstamped observation from %s against an aged-out entry', (_label, make) => {
      const existing = agedOut()
      const merged = mergeQuote(existing, make())

      // Nothing unorderable entered the entry: the stamp is still one the
      // merge can read, which is the invariant stated on `mergeQuotes` and
      // now true of `mergeQuote` on its own.
      expect(merged.at).toBe(STREAM_AT)
      expect(Number.isNaN(Date.parse(merged.at))).toBe(false)
      expect(merged.price).toBe(101)
      expect(merged.source).toBe('stream')
      // The hold is not refreshed either, because nothing was adopted — so
      // the first orderable observation after this still wins immediately
      // rather than starting a fresh fifteen seconds.
      expect(merged.heldSinceLocalMs).toBe(existing.heldSinceLocalMs)
    })

    it('still lets an aged-out entry go to the next stamped observation, older stamp and all', () => {
      // The property the guard must not cost: the age-out exists to release
      // a quiet entry, and it still does for anything the map can order.
      const merged = mergeQuote(agedOut(), barPoll(104))

      expect(merged.price).toBe(104)
      expect(merged.at).toBe(BAR_AT)
      expect(merged.source).toBe('poll')
      expect(merged.heldSinceLocalMs).toBe(Date.now())
    })

    it('leaves the ordinary stamp comparison running until the age-out fires', () => {
      // Inside the window an unstamped observation loses on `-Infinity`,
      // which is the same answer by a different route — pinned so a later
      // reader cannot conclude the guard is the only thing refusing it.
      const held = mergeQuote(undefined, streamedQuote('THIN', 101, STREAM_AT))
      const merged = mergeQuote(held, { ...streamedQuote('THIN', 999, STREAM_AT), at: NO_STAMP })

      expect(merged.price).toBe(101)
      expect(merged.at).toBe(STREAM_AT)
    })
  })

  /** The pin the first version of this fix did not have, and the reason it
   * needed one.
   *
   * That version measured `Date.now() - Date.parse(existing.at)`: a local
   * clock minus a *remote* stamp, so skew did not cancel — it entered the
   * threshold directly, and in both directions. A clock slow by Δ held every
   * entry for `15s + Δ`, so at Δ of minutes the fix barely worked and at Δ
   * of hours (a VM restored from a snapshot, a clock set back) it never
   * fired all session and was a silent no-op. A clock fast by Δ fired it on
   * every call, which does not merely cost the stream its tie-breaks: it
   * switches rule 2 off and lets an *older* observation overwrite a newer
   * one. A held-age is two reads of one clock, so all four cases below hold
   * whatever that clock is doing — and all four fail against the version
   * that measured the stamp. */
  describe('the hold is measured on one clock, so the vendor’s stamp cannot lengthen or shorten it', () => {
    it('holds an entry whose stamp is already hours old, rather than ageing it out on contact', () => {
      // A bar-fallback stamp is hours old the moment it is adopted. Ageing
      // it out on contact is not a freeze, but it is not ordering either:
      // the symbol degrades to last-arrival-wins, so a *genuinely older*
      // observation overwrites a newer one.
      const OLDER_STILL = '2026-09-16T13:00:00Z'

      let map = mergeQuotes({}, [streamedQuote('THIN', 101, BAR_AT)])
      map = mergeQuotes(map, [barPoll(102, OLDER_STILL)])
      expect(map.THIN.price).toBe(101)

      vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
      map = mergeQuotes(map, [barPoll(103, OLDER_STILL)])
      expect(map.THIN.price).toBe(103)
    })

    it('releases an entry stamped in the year 9999, which arithmetic on `at` never bounded', () => {
      // `Date.parse` returns no `+Infinity`, so a parseable far-future stamp
      // gave a large *negative* observation-age, never aged out, and pinned
      // the entry permanently. Same arithmetic as a slow local clock,
      // reached from the other end.
      let map = mergeQuotes({}, [streamedQuote('THIN', 101, '9999-12-31T00:00:00Z')])
      map = mergeQuotes(map, [barPoll(102)])
      expect(map.THIN.price).toBe(101)

      vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
      map = mergeQuotes(map, [barPoll(103)])
      expect(map.THIN.price).toBe(103)
    })

    it('holds for the same fifteen seconds when the local clock is five minutes slow', () => {
      // A resumed laptop, an unsynced VM, w32time drift. Against the vendor
      // stamp every poll lost for 5m15s instead of 15s, and the failure is
      // silent: nothing checks the clock and nothing on screen says the
      // age-out has stopped firing.
      vi.setSystemTime(Date.parse(STREAM_AT) - 5 * 60_000)

      let map = mergeQuotes({}, [streamedQuote('THIN', 101, STREAM_AT)])
      map = mergeQuotes(map, [barPoll(102)])
      expect(map.THIN.price).toBe(101)

      vi.advanceTimersByTime(MAX_HELD_AGE_MS + 1)
      map = mergeQuotes(map, [barPoll(103)])
      expect(map.THIN.price).toBe(103)
    })

    it('keeps rule 2 in force when the local clock runs an hour ahead of the feed', () => {
      // The direction the first version analysed, and misdescribed as
      // "exactly the pre-stream behaviour". Every held entry was old on
      // contact, so `replacesPrice` returned true unconditionally — not a
      // tie-break lost, the whole rule off. Pre-stream there was one writer
      // and last-write-wins was monotone in observation order; with two
      // writers it is an older observation overwriting a newer one, which is
      // the flicker rules 1 and 2 exist to prevent.
      vi.setSystemTime(Date.parse(LATE) + 60 * 60 * 1_000)

      const held = mergeQuotes({}, [streamedQuote('AAPL', 101, LATE)])
      const next = mergeQuotes(held, [polled({ price: 99, at: EARLY })])

      expect(next.AAPL.price).toBe(101)
      expect(next.AAPL.source).toBe('stream')
      // The stale poll still lands its own fields, which never depended on
      // the price race.
      expect(next.AAPL.marketCap).toBe(3_000)
    })
  })
})

describe('mergeQuote — rule 3, the merge is field-level', () => {
  it('a stream write never blanks the poll-only fields', () => {
    const existing = polled({ at: EARLY })
    const merged = mergeQuote(existing, streamed({ price: 105, at: LATE }))

    expect(merged.price).toBe(105)
    expect(merged.previousClose).toBe(99)
    expect(merged.volume).toBe(1_000_000)
    expect(merged.volumeSession).toBe('in_progress')
    expect(merged.volumeDate).toBe('2026-09-16')
    expect(merged.avgVolume).toBe(2_000_000)
    expect(merged.marketCap).toBe(3_000)
  })

  it('a stale poll is discarded for price and applied for everything else', () => {
    const existing = streamed({ price: 105, at: LATE })
    const merged = mergeQuote(
      existing,
      polled({ price: 100, at: EARLY, previousClose: 99, volume: 7, marketCap: 3_100 }),
    )

    expect(merged.price).toBe(105)
    expect(merged.at).toBe(LATE)
    expect(merged.source).toBe('stream')
    expect(merged.previousClose).toBe(99)
    expect(merged.volume).toBe(7)
    expect(merged.marketCap).toBe(3_100)
  })

  it('a poll’s nulls are its answer, not an absence of one', () => {
    const existing = polled({ marketCap: 3_000, avgVolume: 2_000_000 })
    const merged = mergeQuote(existing, polled({ at: LATE, marketCap: null, avgVolume: null }))

    expect(merged.marketCap).toBeNull()
    expect(merged.avgVolume).toBeNull()
  })

  it('a first write for an unseen symbol is taken whole, plus the moment it was taken', () => {
    const incoming = streamed({ price: 42 })
    const merged = mergeQuote(undefined, incoming)

    // Every observation field, unchanged and unstamped by this side.
    expect(merged).toMatchObject(incoming)
    expect(merged.at).toBe(incoming.at)

    // And the one field an entry has that an observation does not.
    // `streamedQuote` could not have supplied it: producers describe
    // observations, and only the merge decides what the map holds.
    expect(merged.heldSinceLocalMs).toBeTypeOf('number')
    expect(merged.heldSinceLocalMs).not.toBe(Date.parse(incoming.at))
  })

  it('an unparseable stamp never beats a real one', () => {
    const existing = polled({ price: 100, at: LATE })
    expect(mergeQuote(existing, streamed({ price: 105, at: 'not a time' })).price).toBe(100)

    const unstamped = polled({ price: 100, at: 'not a time' })
    expect(mergeQuote(unstamped, streamed({ price: 105, at: EARLY })).price).toBe(105)
  })
})

describe('mergeQuotes', () => {
  it('merges per symbol and leaves symbols the batch did not mention alone', () => {
    const map = {
      AAPL: polled({ symbol: 'AAPL', price: 100, at: EARLY }),
      MSFT: polled({ symbol: 'MSFT', price: 400, at: LATE }),
    }
    const next = mergeQuotes(map, [
      polled({ symbol: 'AAPL', price: 101, at: LATE }),
      polled({ symbol: 'NVDA', price: 900, at: LATE }),
    ])

    expect(next.AAPL.price).toBe(101)
    expect(next.MSFT.price).toBe(400)
    expect(next.NVDA.price).toBe(900)
  })

  it('returns a new map rather than mutating the one it was given', () => {
    const map = { AAPL: polled({ price: 100, at: EARLY }) }
    const next = mergeQuotes(map, [polled({ price: 101, at: LATE })])

    expect(map.AAPL.price).toBe(100)
    expect(next).not.toBe(map)
  })
})

describe('mergeQuotes — an observation with no observation time', () => {
  // `request<StockQuote[]>` casts rather than validating, so a server that
  // predates `86a239f` — the commit that put `at` on the REST rows, two
  // commits after `dc09147` shipped `/api/ws` with `WsQuote.at` already
  // required — delivers `undefined` here as a value the types say cannot
  // exist. A browser at HEAD against a server anywhere in that window gets
  // stamped pushes and unstamped polls *together*, which is the sequence
  // the third test below pins.
  function unstamped(over: Partial<LiveQuote> = {}): LiveQuote {
    return polled({ ...over, at: undefined as unknown as string })
  }

  const row: StockQuote = {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    price: 100,
    at: EARLY,
    previousClose: 100,
    volume: 1_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-16',
    avgVolume: 2_000,
    marketCap: 3_000,
  }

  it('does not seed the map, so the table falls back to the query row', () => {
    const next = mergeQuotes({}, [unstamped({ price: 100 })])

    expect(next.AAPL).toBeUndefined()

    // Not a blank and not a fixture: the response's own price, from the
    // very poll the unstamped observation arrived in.
    const [rendered] = liveStockRows([row], next)
    expect(rendered.price).toBe(100)
    expect(rendered.source).toBeNull()
  })

  it('treats an unparseable stamp exactly as an absent one', () => {
    expect(mergeQuotes({}, [polled({ at: 'not a time' })]).AAPL).toBeUndefined()
  })

  it('never leaves a row frozen behind a stamped push', () => {
    // The sequence that motivates the rule: unstamped poll, push with a
    // real stamp, unstamped poll. `-Infinity` loses to every real stamp
    // *forever*, so an entry pinned by one push would discard every later
    // poll for price for the rest of the session while `Markets.tsx`'s
    // `Read HH:MM:SS ET` header — which advances on any successful fetch —
    // went on claiming the row was current.
    let map = mergeQuotes({}, [unstamped({ price: 100 })])
    map = mergeQuotes(map, [streamedQuote('AAPL', 101, LATE)])
    expect(map.AAPL.price).toBe(101)

    map = mergeQuotes(map, [unstamped({ price: 102 })])
    expect(map.AAPL).toBeUndefined()

    const [rendered] = liveStockRows([{ ...row, price: 102 }], map)
    expect(rendered.price).toBe(102)
    // Derived from the row's own basis, so the pair on screen still agree.
    expect(rendered.change).toBe(2)
    expect(rendered.source).toBeNull()
  })

  it('touches no symbol but its own, and holds only entries it can order', () => {
    const map = mergeQuotes(
      {
        AAPL: polled({ price: 100, at: EARLY }),
        MSFT: polled({ symbol: 'MSFT', price: 400, at: LATE }),
      },
      [unstamped({ price: 102 })],
    )

    expect(Object.keys(map)).toEqual(['MSFT'])
    for (const quote of Object.values(map)) {
      expect(Number.isNaN(Date.parse(quote.at))).toBe(false)
    }
  })

  it('takes a stamped poll the moment one arrives, over a pushed entry', () => {
    let map = mergeQuotes({}, [streamedQuote('AAPL', 101, EARLY)])
    map = mergeQuotes(map, [polled({ price: 103, at: LATE })])

    expect(map.AAPL.price).toBe(103)
    expect(map.AAPL.source).toBe('poll')
  })

  /** WEB-2. The `delete` above was written for the poll, where the row the
   * table falls back to arrived in the very same response. A malformed
   * *stream* frame has no such row behind it: the entry it would evict was
   * written by a different, healthy writer, so a bad frame took a good
   * price off the screen — and `markStreamed` stamped `lastTickAt` anyway,
   * so the pill went on reading `Live` with nothing saying the row had
   * just lost its live entry. */
  describe('from the stream, where the entry belongs to the other writer', () => {
    function unstampedPush(price: number): LiveQuote {
      return { ...streamedQuote('AAPL', price, EARLY), at: undefined as unknown as string }
    }

    it('leaves a healthy polled entry exactly where it was', () => {
      const map = { AAPL: polled({ price: 100, at: EARLY, marketCap: 3_000 }) }
      const next = mergeQuotes(map, [unstampedPush(999)])

      // Untouched, not re-merged: the frame is evidence about the frame.
      expect(next.AAPL).toBe(map.AAPL)
      expect(next.AAPL.price).toBe(100)
      expect(next.AAPL.source).toBe('poll')
      expect(next.AAPL.marketCap).toBe(3_000)
    })

    it('does not seed an entry for a symbol the map has never seen', () => {
      expect(mergeQuotes({}, [unstampedPush(101)])).toEqual({})
    })

    it('leaves the poll writing that entry on the next cycle', () => {
      let map = mergeQuotes({}, [polled({ price: 100, at: EARLY })])
      map = mergeQuotes(map, [unstampedPush(999)])
      map = mergeQuotes(map, [polled({ price: 102, at: LATE })])

      expect(map.AAPL.price).toBe(102)
    })

    it('still removes the entry when the unorderable observation is the poll’s', () => {
      // The asymmetry is the point, so pin both halves together.
      const map = { AAPL: streamed({ price: 101, at: EARLY }) }

      expect(mergeQuotes(map, [unstampedPush(999)]).AAPL).toBeDefined()
      expect(mergeQuotes(map, [polled({ at: undefined as unknown as string })]).AAPL).toBeUndefined()
    })
  })
})

describe('the two producers', () => {
  const row: StockQuote = {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    price: 101.5,
    at: EARLY,
    previousClose: 100,
    volume: 1_000,
    volumeSession: 'in_progress',
    volumeDate: '2026-09-16',
    avgVolume: 2_000,
    marketCap: 3_000,
  }

  it('reads a stock row’s previous close outright, never as price − change', () => {
    expect(liveFromStockQuote(row).previousClose).toBe(100)
    expect(liveFromStockQuote(row).source).toBe('poll')
    expect(liveFromStockQuote(row).at).toBe(EARLY)
  })

  it('carries a null previous close through as a null basis, never a zero one', () => {
    expect(liveFromStockQuote({ ...row, previousClose: null }).previousClose).toBeNull()
  })

  it('treats an absent previous close as null, not as undefined', () => {
    // `!= null`, not `!== null` — `markets.ts#latestVolumeDate` states the
    // rule. An older server omits the key entirely and `request<T>` casts
    // rather than validating, so `undefined` reaches here as a value the
    // types say cannot exist. Under a strict check it sails past
    // `changeOf`'s guard and the column renders `$NaN` instead of the em
    // dash that says there is no basis.
    const { previousClose: _omitted, ...withoutBasis } = row
    const live = liveFromStockQuote(withoutBasis as StockQuote)

    expect(live.previousClose).toBeNull()
    expect(changeOf(live)).toBeNull()
    expect(changePctOf(live)).toBeNull()
  })

  it('reads an underlying row’s previous close outright and carries no volume', () => {
    const quote: UnderlyingQuote = {
      symbol: 'AAPL',
      price: 101.5,
      at: EARLY,
      previousClose: 100,
      history: [],
      intraday: [],
    }
    const live = liveFromUnderlyingQuote(quote)

    expect(live.previousClose).toBe(100)
    expect(live.source).toBe('poll')
    expect(live.volume).toBeNull()
    expect(live.avgVolume).toBeNull()
    expect(live.marketCap).toBeNull()
  })

  it('a streamed quote carries a price and nothing else', () => {
    const live = streamedQuote('AAPL', 101.5, EARLY)

    expect(live).toEqual({
      symbol: 'AAPL',
      price: 101.5,
      at: EARLY,
      source: 'stream',
      previousClose: null,
      volume: null,
      volumeSession: null,
      volumeDate: null,
      avgVolume: null,
      marketCap: null,
    })
  })
})

describe('rule 4 — change is derived, never stored', () => {
  it('measures the move from the previous close', () => {
    expect(changeOf(polled({ price: 101.5, previousClose: 100 }))).toBeCloseTo(1.5, 10)
    expect(changePctOf(polled({ price: 101.5, previousClose: 100 }))).toBeCloseTo(1.5, 10)
  })

  it('is negative on a down day, on both selectors', () => {
    expect(changeOf(polled({ price: 98, previousClose: 100 }))).toBeCloseTo(-2, 10)
    expect(changePctOf(polled({ price: 98, previousClose: 100 }))).toBeCloseTo(-2, 10)
  })

  it('returns null with no basis to measure from — never 0', () => {
    expect(changeOf(polled({ previousClose: null }))).toBeNull()
    expect(changePctOf(polled({ previousClose: null }))).toBeNull()
  })

  it('returns null on a zero previous close rather than dividing by it', () => {
    expect(changePctOf(polled({ price: 5, previousClose: 0 }))).toBeNull()
  })

  it('follows a streamed price, which is the whole point of deriving it', () => {
    const merged = mergeQuote(
      polled({ price: 100, previousClose: 100, at: EARLY }),
      streamed({ price: 102, at: LATE }),
    )

    expect(changeOf(merged)).toBeCloseTo(2, 10)
    expect(changePctOf(merged)).toBeCloseTo(2, 10)
  })
})

/** The previous-close guard, relocated from the server.
 *
 * It was `tests/api/test_markets_routes.py::
 * test_the_change_is_measured_from_the_previous_close`, which asserted the
 * `change` the row used to carry. Decision 18 took that field off the wire,
 * so the subtraction happens *here* now and the guard has to live where the
 * subtraction does. The server still owns getting the basis right and still
 * tests that; this owns measuring from it and from nothing else.
 *
 * **Why it is worth a test of its own: every wrong basis yields a
 * plausible-looking number.** NVDA is down 5.95 on the recording these
 * figures come from. Its 60-session series opens at 206.64 and closes at
 * 225.16, and today's partial daily bar sits at 217.90 — anchored to any of
 * those the row reports a confident move, in the right units, with a sign
 * and two decimal places, and nothing downstream can tell it is wrong. A
 * crash is visible; +5.41% on a day the stock fell is not. Only 223.77 is
 * yesterday's settle. */
describe('rule 4 — the basis is yesterday’s settle, never a point of a series', () => {
  /** The quote mid, which is what the server prices the row at. */
  const PRICE = 217.82
  const SETTLE = 223.77
  /** The three plausible wrong answers, each named. */
  const SERIES_FIRST = 206.64
  const SERIES_LAST = 225.16
  const TODAYS_PARTIAL_BAR = 217.9

  it('measures from the previous close, reporting the recorded down move', () => {
    expect(changeOf({ price: PRICE, previousClose: SETTLE })).toBe(-5.95)
    // ≈ −2.66%, and over the *snapped* dollars, so the percent and the
    // dollars beside it are the same move.
    expect(changePctOf({ price: PRICE, previousClose: SETTLE })).toBeCloseTo(
      (-5.95 / SETTLE) * 100,
      10,
    )
  })

  it('does not measure from the first point of the series', () => {
    // The left edge of a 60-session chart is two months ago, and "today"
    // measured against it is not today. It would print +$11.18 / +5.41% on
    // a day the stock fell six dollars.
    const wrong = changeOf({ price: PRICE, previousClose: SERIES_FIRST })
    expect(wrong).toBeCloseTo(11.18, 10)
    expect(changeOf({ price: PRICE, previousClose: SETTLE })).not.toBe(wrong)
  })

  it('does not measure from the last point of the series, nor from today’s own bar', () => {
    // The series' last point is today's live price, so it would report a
    // move against itself; today's partial daily bar is close enough to
    // the settle to print a near-flat −$0.08 that reads as a quiet session
    // rather than as a bug.
    expect(changeOf({ price: PRICE, previousClose: SERIES_LAST })).toBeCloseTo(-7.34, 10)
    expect(changeOf({ price: PRICE, previousClose: TODAYS_PARTIAL_BAR })).toBeCloseTo(-0.08, 10)

    const right = changeOf({ price: PRICE, previousClose: SETTLE })
    expect(right).not.toBe(changeOf({ price: PRICE, previousClose: SERIES_LAST }))
    expect(right).not.toBe(changeOf({ price: PRICE, previousClose: TODAYS_PARTIAL_BAR }))
  })

  it('never measures from a stream’s own first observed price', () => {
    // `streamedQuote` carries `previousClose: null` deliberately: a push
    // carries a price and nothing else, so a symbol the websocket reports
    // before any poll has *no basis*. Treating the first price seen as the
    // basis would print a flat `+$0.00` on the first tick and a move
    // measured from an arbitrary intraday instant on every one after — both
    // plausible, neither yesterday's settle.
    const first = streamedQuote('NVDA', PRICE, EARLY)
    expect(first.previousClose).toBeNull()
    expect(changeOf(first)).toBeNull()
    expect(changePctOf(first)).toBeNull()

    const second = mergeQuote(first, streamedQuote('NVDA', 220, LATE))
    expect(second.price).toBe(220)
    expect(second.previousClose).toBeNull()
    expect(changeOf(second)).toBeNull()
    expect(changePctOf(second)).toBeNull()
  })

  it('measures a pushed price against the poll’s basis, not against the poll’s price', () => {
    // The whole reason the pair is derived: the change beside a streamed
    // price has to move with it. Read off the poll it would still say
    // −$5.95 next to a price that is now up on the day.
    const merged = mergeQuote(
      polled({ symbol: 'NVDA', price: PRICE, previousClose: SETTLE, at: EARLY }),
      streamedQuote('NVDA', 225, LATE),
    )

    expect(merged.previousClose).toBe(SETTLE)
    expect(changeOf(merged)).toBe(1.23)
    expect(changePctOf(merged)).toBeCloseTo((1.23 / SETTLE) * 100, 10)
  })
})

describe('liveStockRows', () => {
  const rows: StockQuote[] = [
    {
      symbol: 'AAPL',
      name: 'Apple Inc.',
      price: 100,
      at: EARLY,
      previousClose: 99,
      volume: 1_000,
      volumeSession: 'in_progress',
      volumeDate: '2026-09-16',
      avgVolume: 2_000,
      marketCap: 3_000,
    },
    {
      symbol: 'SPY',
      name: 'SPDR S&P 500 ETF Trust',
      price: 500,
      at: EARLY,
      previousClose: null,
      volume: null,
      volumeSession: null,
      volumeDate: null,
      avgVolume: null,
      marketCap: null,
    },
  ]

  it('renders the live price and a change derived from it where an entry exists', () => {
    const live = liveStockRows(rows, {
      AAPL: streamed({ symbol: 'AAPL', price: 103, at: LATE, previousClose: 99 }),
    })

    expect(live[0].price).toBe(103)
    expect(live[0].change).toBeCloseTo(4, 10)
    expect(live[0].changePct).toBeCloseTo((4 / 99) * 100, 10)
    expect(live[0].source).toBe('stream')
  })

  it('renders the query row’s own price where there is no live entry', () => {
    const live = liveStockRows(rows, {})

    expect(live[0].price).toBe(100)
    expect(live[0].change).toBeCloseTo(1, 10)
    expect(live[0].source).toBeNull()
  })

  it('measures a stream-before-poll row against the query row’s own basis', () => {
    // The ordering the browser WS client will produce on a cold start: the
    // socket reports a symbol before the 400ms poll has landed, so the map
    // entry is a `streamedQuote` and carries `previousClose: null`. Reading
    // only the entry blanked the Change cell and gave the reason "no
    // previous daily bar to measure the move from", which is false — the
    // row on screen carries the basis. A blank cell for under one poll
    // interval is survivable; a stated reason that is untrue is not.
    const live = liveStockRows(rows, {
      AAPL: streamedQuote('AAPL', 103, LATE),
    })

    expect(live[0].source).toBe('stream')
    expect(live[0].price).toBe(103)
    expect(live[0].change).toBeCloseTo(4, 10)
    expect(live[0].changePct).toBeCloseTo((4 / 99) * 100, 10)
  })

  it('still reports no basis where neither the entry nor the row has one', () => {
    const live = liveStockRows(rows, { SPY: streamedQuote('SPY', 505, LATE) })

    expect(live[1].price).toBe(505)
    expect(live[1].change).toBeNull()
    expect(live[1].changePct).toBeNull()
  })

  it('keeps a missing change missing rather than printing a flat day', () => {
    const live = liveStockRows(rows, {})

    expect(live[1].change).toBeNull()
    expect(live[1].changePct).toBeNull()
  })

  it('leaves the poll-only columns exactly as the query row had them', () => {
    const live = liveStockRows(rows, { AAPL: streamed({ symbol: 'AAPL', price: 103, at: LATE }) })

    expect(live[0].volume).toBe(1_000)
    expect(live[0].avgVolume).toBe(2_000)
    expect(live[0].marketCap).toBe(3_000)
    expect(live[0].name).toBe('Apple Inc.')
  })

  it('does not mutate the rows it was given', () => {
    const live = liveStockRows(rows, { AAPL: streamed({ symbol: 'AAPL', price: 103, at: LATE }) })

    expect(rows[0].price).toBe(100)
    expect(live[0]).not.toBe(rows[0])
  })
})
