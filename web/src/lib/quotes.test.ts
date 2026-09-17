import { describe, it, expect } from 'vitest'
import type { LiveQuote, StockQuote, UnderlyingQuote } from './types'
import {
  changeOf,
  changePctOf,
  liveFromStockQuote,
  liveFromUnderlyingQuote,
  liveStockRows,
  mergeQuote,
  mergeQuotes,
  streamedQuote,
} from './quotes'

const EARLY = '2026-09-16T14:30:00Z'
const LATE = '2026-09-16T14:30:01Z'

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

  it('a first write for an unseen symbol is taken whole', () => {
    const incoming = streamed({ price: 42 })
    expect(mergeQuote(undefined, incoming)).toEqual(incoming)
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
