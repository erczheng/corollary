import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { downloadCsv } from './csv'

/** `downloadCsv` builds a Blob and clicks an anchor, so the assertions run
 * against those two side effects: what went into the file, and whether the
 * object URL was handed back afterwards.
 *
 * jsdom implements neither `URL.createObjectURL` nor `Blob.text()`, so both
 * are stubbed — the blob's contents are captured on the way in instead. */
let captured: string[]
let revoked: string[]
let clicks: { download: string; href: string }[]

beforeEach(() => {
  captured = []
  revoked = []
  clicks = []

  vi.stubGlobal(
    'Blob',
    class {
      constructor(parts: string[]) {
        captured.push(parts.join(''))
      }
    },
  )

  URL.createObjectURL = vi.fn(() => 'blob:corollary/1') as unknown as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn((url: string) => {
    revoked.push(url)
  }) as unknown as typeof URL.revokeObjectURL

  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicks.push({ download: this.download, href: this.href })
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const text = () => captured[0]
const lines = () => text().split('\n')

describe('downloadCsv', () => {
  it('writes a header row from the first row’s keys, then a row each', () => {
    downloadCsv('trades.csv', [
      { symbol: 'AAPL', qty: 2 },
      { symbol: 'SPY', qty: 3 },
    ])

    expect(lines()).toEqual(['"symbol","qty"', '"AAPL","2"', '"SPY","3"'])
  })

  it('names the file and releases the object URL', () => {
    downloadCsv('trades.csv', [{ symbol: 'AAPL' }])

    expect(clicks).toHaveLength(1)
    expect(clicks[0].download).toBe('trades.csv')
    expect(revoked).toEqual(['blob:corollary/1'])
  })

  /** Nothing to export is not an empty file — an empty file downloaded
   * silently looks like a broken export. */
  it('does nothing at all for an empty set', () => {
    downloadCsv('trades.csv', [])

    expect(captured).toHaveLength(0)
    expect(clicks).toHaveLength(0)
  })

  /** The reason every field is quoted. A contract description carries
   * commas, and an unquoted one would shift every column after it. */
  it('quotes a field containing a comma so columns do not shift', () => {
    downloadCsv('t.csv', [{ asset: 'SPY $430/$425 Put Credit Spread, Oct 17', qty: 3 }])

    expect(lines()[1]).toBe('"SPY $430/$425 Put Credit Spread, Oct 17","3"')
    expect(lines()[1].split('","')).toHaveLength(2)
  })

  /** RFC 4180: a literal quote is escaped by doubling it. Left raw, it would
   * terminate the field early and corrupt the rest of the row. */
  it('escapes a literal quote by doubling it', () => {
    downloadCsv('t.csv', [{ note: 'he said "buy"' }])

    expect(lines()[1]).toBe('"he said ""buy"""')
  })

  it('keeps a newline inside a quoted field', () => {
    downloadCsv('t.csv', [{ note: 'line one\nline two' }])

    // Three physical lines: header, then a field spanning two of them.
    expect(lines()).toHaveLength(3)
    expect(text()).toContain('"line one\nline two"')
  })

  /** A null confidence exports empty rather than as the em dash it renders
   * as — "—" in a numeric column is a parse error (PRD.md §8.5). This is the
   * helper half of that: a missing key does not become "undefined". */
  it('writes an empty field for a missing value', () => {
    downloadCsv('t.csv', [
      { symbol: 'AAPL', confidence: 71 },
      { symbol: 'AMD' } as Record<string, string | number>,
    ])

    expect(lines()[2]).toBe('"AMD",""')
  })

  /** Columns come from the first row, so a later row's extra key is not a
   * new column appearing halfway down the file. */
  it('takes its columns from the first row only', () => {
    downloadCsv('t.csv', [{ a: 1 }, { a: 2, b: 3 } as Record<string, string | number>])

    expect(lines()[0]).toBe('"a"')
    expect(lines()[2]).toBe('"2"')
  })

  it('preserves zero rather than treating it as missing', () => {
    downloadCsv('t.csv', [{ pnl: 0 }])

    expect(lines()[1]).toBe('"0"')
  })
})
