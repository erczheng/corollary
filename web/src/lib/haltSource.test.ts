import { describe, expect, it } from 'vitest'

/** Spec decision C12 (Phase 3 step 8, U10): **one halt in the application.**
 *
 * The store's `isHalted` is the Phase 1 fixture halt. Nothing in production
 * writes it, so a surface that reads it offers to open a position while the
 * engine is halted. Research, the chain ticket and the command palette all
 * did, until U10. *"Two halts in one application is the exact divergence
 * that produced four Phase 2 bugs"* (PRD §11).
 *
 * The rule is an absence, and an absence is what a plausible-looking diff
 * adds to, so it is checked at the source level, the way
 * `liveSocket.test.ts` checks that the socket never halts or resumes. The
 * engine's halt comes from `useEngineState()` or `useEntryGate()`.
 *
 * `lib/store.ts` still holds the field and its `halt()`/`resume()`, because
 * `flatten()`'s test is where rule 7's halt-versus-flatten distinction is
 * written down; pruning the store is Phase 6's question. Tests may read it.
 *
 * **What this does not catch, stated rather than chased:** a selector
 * exported from the store itself and imported by a page, since the store is
 * the allowed file; and a read written on a line that begins with `*` or
 * `/*`, which the comment filter drops. It polices an absence. It cannot
 * prove a surface that opens a position *uses* `useEntryGate()`; that is a
 * review question for each new surface. */

const GLOBBED = import.meta.glob<string>(
  ['../**/*.{ts,tsx}', '!../**/*.test.{ts,tsx}', '!../test/**'],
  { query: '?raw', import: 'default', eager: true },
)

/** Keyed by path under `src/`. Vite names a file beside this one `./x.ts`
 * and every other `../dir/x.ts`, so one file could otherwise go by two
 * names and slip past `ALLOWED`. */
const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(GLOBBED).map(([key, source]) => [
    key.startsWith('./') ? `lib/${key.slice(2)}` : key.replace(/^\.\.\//, ''),
    source,
  ]),
)

/** Where the field is allowed to appear: its own definition. */
const ALLOWED = new Set(['lib/store.ts'])

/** Comment lines are prose, not reads, and the store's history is written in
 * them. Same filter as `liveSocket.test.ts`. */
function codeOf(source: string): string {
  return source
    .split('\n')
    .filter((line) => !/^\s*(\*|\/\*|\/\/)/.test(line))
    .join('\n')
}

/** Every shape a read of the field can take: a property access (`s.isHalted`,
 * `getState().isHalted`), an indexed one (`s['isHalted']`) and a
 * destructure or literal (`{ isHalted }`, `{ isHalted: h }`). */
const READS = [/\.isHalted\b/, /\[\s*['"`]isHalted['"`]\s*\]/, /[{,]\s*isHalted\s*[,}:=]/]

export function readsFixtureHalt(source: string): boolean {
  const code = codeOf(source)
  return READS.some((pattern) => pattern.test(code))
}

describe('the halt has one source', () => {
  it('scans the real tree, not nothing', () => {
    const files = Object.keys(SOURCES)
    // These are what fail if the glob ever matches nothing, so the check
    // below cannot pass vacuously.
    expect(files.length).toBeGreaterThan(50)
    expect(files).toContain('pages/Research.tsx')
    expect(files).toContain('components/ChainOrderTicket.tsx')
    expect(files).toContain('components/CommandPalette.tsx')
    expect(files).toContain('lib/store.ts')
    expect(files).toContain('lib/entryGate.ts')
    expect(files.some((f) => f.includes('.test.'))).toBe(false)
  })

  it('no production module outside the store reads the fixture halt', () => {
    const offenders = Object.entries(SOURCES)
      .filter(([file, source]) => !ALLOWED.has(file) && readsFixtureHalt(source))
      .map(([file]) => file)

    expect(offenders).toEqual([])
  })

  /** The detector itself, against planted reads, so a pattern that stops
   * matching cannot turn the check above into a pass. */
  describe('the detector', () => {
    it.each([
      ['a selector', 'const h = useUIStore((s) => s.isHalted)'],
      ['getState', 'if (useUIStore.getState().isHalted) return'],
      ['an indexed read', "const h = useUIStore((s) => s['isHalted'])"],
      ['a destructure', 'const { isHalted } = useUIStore()'],
      ['a renamed destructure', 'const { executionMode, isHalted: h } = useUIStore()'],
      ['a defaulted destructure', 'const { isHalted = false } = useUIStore.getState()'],
      ['a write', 'useUIStore.setState({ isHalted: true })'],
    ])('catches %s', (_name, planted) => {
      expect(readsFixtureHalt(`export function Page() {\n  ${planted}\n}`)).toBe(true)
    })

    it.each([
      ['a comment line', '  // the store\'s `isHalted` is the fixture halt'],
      ['a doc comment line', ' * reads `isHalted` from the store, which is wrong'],
      ['the engine halt', 'const halted = engineQuery.data?.halted ?? false'],
      ['the gate', 'const gate = useEntryGate()'],
      ['a different name', 'const isHaltedByEngine = gate.kind === "halted"'],
    ])('passes %s', (_name, line) => {
      expect(readsFixtureHalt(line)).toBe(false)
    })
  })
})
