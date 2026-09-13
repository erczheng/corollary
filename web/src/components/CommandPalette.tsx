import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useUIStore } from '../lib/store'
import { useEngineState, useHaltEngine, useResumeEngine } from '../lib/queries'
import { RECOMMENDATIONS, recommendationTitle } from '../lib/mockData'
import { dispositionOf, visibleRecommendations } from '../lib/research'
import { DESTINATIONS } from '../lib/routes'

export interface Command {
  id: string
  label: string
  group: string
  /** Secondary text on the right — what running it will do, where a label
   * alone is ambiguous. */
  hint?: string
  run: () => void
}

/** Case-insensitive substring match on label and group.
 *
 * Deliberately not fuzzy. Fuzzy matching earns its keep on a list of hundreds;
 * on a list of this size it mostly means a typo silently selects a *different*
 * command, and some of these commands halt the engine. Exported for its test. */
export function filterCommands(commands: Command[], query: string): Command[] {
  const q = query.trim().toLowerCase()
  if (q === '') return commands
  return commands.filter(
    (c) => c.label.toLowerCase().includes(q) || c.group.toLowerCase().includes(q),
  )
}

/** Tone of a notice. `caution` is a capability this phase does not have,
 * `error` is a request that actually failed, `info` is a write that landed.
 * A Phase 2 limitation is **not** an error — nothing is broken. */
type NoticeTone = 'caution' | 'error' | 'info'

interface Notice {
  title: string
  body: string
  tone: NoticeTone
}

const NOTICE_TONE_CLASS: Record<NoticeTone, string> = {
  caution: 'text-caution',
  error: 'text-error',
  info: 'text-on-surface',
}

/** The reason a halt taken from the palette records. Rule 8: a state change
 * carries who asked for it, not just that it happened, and "Ctrl+K" is a
 * materially different answer from "the dead-man's switch". */
const HALT_REASON = 'Halted by hand from the command palette'

/** Phase 2 is read-only. Closing a position is an order, every order goes
 * through the risk manager (rule 1), and the risk manager arrives in Phase 6.
 *
 * The wording deliberately mirrors `Dashboard.tsx`'s `FLATTEN_UNAVAILABLE_REASON`
 * — the same fact should not be explained two different ways depending on
 * which surface you reached for. It is restated rather than imported because
 * the constant is page-local and a component importing from a page is a worse
 * coupling than a duplicated sentence; when this moves to `lib/`, both move. */
const FLATTEN_UNAVAILABLE: Notice = {
  title: 'Flatten is unavailable — nothing was closed',
  body:
    'Flatten is disabled in this phase. Closing a position is an order, every order has to go ' +
    'through the risk manager, and that arrives in Phase 6 — a Flatten that silently did nothing ' +
    'would leave you believing you were flat. Your positions are still open. Close at Alpaca in ' +
    'the meantime.',
  tone: 'caution',
}

const HALTED: Notice = {
  title: 'Engine halted',
  body:
    'New entries are stopped. Open positions keep their managed exits and nothing was closed — ' +
    'flattening is a separate control, and in this phase an unavailable one.',
  tone: 'info',
}

const RESUMED: Notice = {
  title: 'Engine resumed',
  body: 'The halt is over. The engine may open new positions again.',
  tone: 'info',
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A write that did not land. `error`, not `caution`: this one is a fault.
 *
 * It names what the engine's state is *not*, because the hazard of a failed
 * halt is believing you took it. */
function writeFailed(action: 'halt' | 'resume', error: unknown): Notice {
  return {
    title: action === 'halt' ? 'Halt request failed' : 'Resume request failed',
    body:
      `${messageOf(error)} The engine state is unchanged — it is still ` +
      `${action === 'halt' ? 'running' : 'halted'}. Check the Dashboard before assuming otherwise.`,
    tone: 'error',
  }
}

/** What the palette says after it has closed itself.
 *
 * Running a command closes the palette first, so by the time a command has
 * anything to report there is no palette left to report it in. This is that
 * place: one dialog, rendered by the parent **outside** its `open` conditional,
 * for the same structural reason the flatten confirm was (see below).
 *
 * The `null` early return lives *here*, inside the child, exactly the way
 * `ConfirmDialog` does it — that is safe. The bug was the early return in the
 * **parent**, above this element. */
function PaletteNotice({ notice, onDismiss }: { notice: Notice | null; onDismiss: () => void }) {
  const dismissRef = useRef<HTMLButtonElement>(null)

  // The palette closed and took focus with it, so focus lands on <body> with
  // nothing to press. Move it onto the one control this dialog has.
  useEffect(() => {
    if (notice !== null) dismissRef.current?.focus()
  }, [notice])

  useEffect(() => {
    if (notice === null) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onDismiss()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [notice, onDismiss])

  if (notice === null) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-inverse-surface/40 p-6"
      role="presentation"
      onClick={onDismiss}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="palette-notice-title"
        aria-describedby="palette-notice-body"
        className="w-full max-w-md rounded-xl border border-outline-warm bg-surface-container-lowest p-6 text-on-surface"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="palette-notice-title" className={`text-title-lg ${NOTICE_TONE_CLASS[notice.tone]}`}>
          {notice.title}
        </h2>
        <p id="palette-notice-body" className="mt-3 text-body-md text-on-surface-variant">
          {notice.body}
        </p>
        <div className="mt-6 flex justify-end">
          <button
            type="button"
            ref={dismissRef}
            onClick={onDismiss}
            className="rounded border border-outline px-4 py-2 text-label-md text-on-surface"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  )
}

/** Ctrl+K palette — PRD.md §11 names it in the Phase 1 bar.
 *
 * Three groups, which is what the placeholder here promised: jump to a page,
 * act on a recommendation, control the engine.
 *
 * **Halt and Flatten stay separate entries** for the same reason they are
 * separate functions everywhere else (CLAUDE.md rule 7): one stops new
 * entries, the other closes everything. In this phase that difference is
 * sharper than usual — Halt is a real write (`POST /api/engine/halt`), and
 * Flatten cannot happen at all.
 *
 * **Flatten is listed, and says why it cannot run.** It used to call
 * `store.flatten()`, which emptied a fixture book that no page renders any
 * more; the confirm said "this cannot be undone", you accepted it, and not one
 * real position moved. A destructive command that reports success and does
 * nothing is the worst state of the three available.
 *
 * The alternative was to omit it, the way Execute is omitted while the engine
 * is halted. That rule is about a command that *silently* does nothing, and it
 * fits Execute: the reason is transient, visible elsewhere, and the command
 * returns within the session. Flatten is the opposite on every count. It is
 * the control you reach for in a hurry, its absence lasts until Phase 6, and
 * "No command matches “flatten”" answers none of the questions you have — you
 * would go looking for another route to it, and the one thing you must not
 * conclude is that you are flat. So it stays findable, the row itself says
 * `Unavailable until Phase 6`, and running it states the consequence in the
 * concrete terms a destructive confirm would have: your positions are still
 * open, close them at Alpaca.
 */
export function CommandPalette() {
  const open = useUIStore((s) => s.paletteOpen)
  const close = useUIStore((s) => s.closePalette)
  const navigate = useNavigate()

  // The engine's halt is server state, and the only copy of it. The store's
  // `isHalted` is the Phase 1 fixture halt — the Dashboard's pill reads the
  // API now, so a palette Halt that flipped the store would have been the
  // same silent no-op as the flatten below it.
  const engineQuery = useEngineState()
  const engineHalted = engineQuery.data?.halted ?? false
  const { mutate: mutateHalt } = useHaltEngine()
  const { mutate: mutateResume } = useResumeEngine()

  // Still the fixture halt, because the recommendations it gates are still
  // fixtures: `Research.tsx` reads this same flag over this same list, and a
  // palette that gated them on a different halt than the page they live on
  // would disagree with it. Or-ed with the engine's real halt so the gate can
  // only ever be *more* conservative — halting from this very palette must
  // stop the Execute rows underneath it in the same breath. Both sides of
  // this move to the API together when recommendations do.
  const fixtureHalted = useUIStore((s) => s.isHalted)
  const newEntriesStopped = engineHalted || fixtureHalted
  const dispositions = useUIStore((s) => s.dispositions)
  const executeRecommendation = useUIStore((s) => s.executeRecommendation)
  const dismissRecommendation = useUIStore((s) => s.dismissRecommendation)

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const [notice, setNotice] = useState<Notice | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Every engine command ends in a stated outcome, success or failure. The
  // palette is reachable from every page and closes before the request
  // settles, so without this a halt taken from Markets would report nothing
  // either way — and a halt you believe you took is the failure rule 9 exists
  // to keep visible.
  const runHalt = useCallback(() => {
    mutateHalt(HALT_REASON, {
      onSuccess: () => setNotice(HALTED),
      onError: (error) => setNotice(writeFailed('halt', error)),
    })
  }, [mutateHalt])

  // Rule 9: a human action, never an automatic one. A keystroke someone typed
  // is exactly that; nothing here retries or resumes on reconnect.
  const runResume = useCallback(() => {
    mutateResume(undefined, {
      onSuccess: () => setNotice(RESUMED),
      onError: (error) => setNotice(writeFailed('resume', error)),
    })
  }, [mutateResume])

  const commands = useMemo<Command[]>(() => {
    const list: Command[] = DESTINATIONS.map((page) => ({
      id: `page-${page.to}`,
      label: `Go to ${page.label}`,
      group: 'Pages',
      run: () => navigate(page.to),
    }))

    // Only candidates still on the board. Offering to execute something you
    // already dismissed would be acting on a list you cannot see.
    for (const r of visibleRecommendations(RECOMMENDATIONS, dispositions)) {
      const title = recommendationTitle(r)
      const acted = dispositionOf(r.id, dispositions) !== 'open'
      // Gated on the halt, which the palette was not honouring: halting
      // stops new entries (CLAUDE.md rule 7), and a surface that still
      // offers to open one is the surface it happens on by accident.
      // Omitted rather than shown-disabled — there is no disabled state in
      // a command list, and a command that silently does nothing is worse
      // than an absent one.
      if (!acted && !newEntriesStopped) {
        list.push({
          id: `exec-${r.id}`,
          label: `Execute ${title}`,
          group: 'Recommendations',
          hint: 'Submits to the risk manager',
          run: () => executeRecommendation(r.id),
        })
      }
      // Never gated on the halt: waving off a candidate opens nothing.
      list.push({
        id: `dismiss-${r.id}`,
        label: `Dismiss ${title}`,
        group: 'Recommendations',
        run: () => dismissRecommendation(r.id),
      })
    }

    // Which one of the pair to show is a question about the engine, and until
    // the engine answers it, neither is offered. Guessing the wrong one is not
    // symmetric with a brief absence: offering Halt to an already-halted
    // engine overwrites `haltedReason` and `haltedAt`, which is how the
    // dead-man's switch's account of itself gets replaced by "Halted by hand".
    // The window is the first paint of the app — the palette mounts with it —
    // and a *failed* engine read is not this case: that falls through to Halt
    // below, the conservative direction, and fails loudly if it cannot land.
    if (!engineQuery.isPending) {
      list.push(
        engineHalted
          ? {
              id: 'resume',
              label: 'Resume trading',
              group: 'Engine',
              hint: 'Allows new entries again',
              run: runResume,
            }
          : {
              id: 'halt',
              label: 'Halt trading',
              group: 'Engine',
              hint: 'Stops new entries — open positions keep their exits',
              run: runHalt,
            },
      )
    }

    list.push({
      id: 'flatten',
      label: 'Flatten all positions',
      group: 'Engine',
      // Stated on the row, before Enter, as well as in the dialog after it.
      hint: 'Unavailable until Phase 6 — closes nothing',
      run: () => setNotice(FLATTEN_UNAVAILABLE),
    })

    return list
  }, [
    navigate,
    dispositions,
    executeRecommendation,
    dismissRecommendation,
    newEntriesStopped,
    engineHalted,
    engineQuery.isPending,
    runHalt,
    runResume,
  ])

  const results = filterCommands(commands, query)
  // Clamped rather than remembered: after filtering, index 7 may not exist.
  const activeIndex = Math.min(selected, Math.max(results.length - 1, 0))
  const active = results[activeIndex]

  useEffect(() => {
    if (open) inputRef.current?.focus()
  }, [open])

  // A fresh palette every time. Reopening onto the last query — and the last
  // highlighted command — is how Enter runs something you did not read.
  useEffect(() => {
    if (!open) {
      setQuery('')
      setSelected(0)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') close()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, close])

  function runActive() {
    if (!active) return
    // Closes first so the page underneath is what you see the result on. The
    // flatten confirm is rendered outside this block for exactly that reason.
    close()
    active.run()
  }

  return (
    <>
      {/* The palette body is conditional; the notice below is NOT.
          `if (!open) return null` used to sit above this return, which meant
          running Flatten — whose first act is to close the palette — unmounted
          its dialog in the same tick it was asked for. The dialog could never
          appear, so the most destructive command in the list was the one with
          no guard behind it. The dialog's contents have changed; the hazard
          has not, and it now covers halt and resume too. */}
      {open ? (
      <div
        className="fixed inset-0 z-50 flex items-start justify-center pt-32"
        role="presentation"
        onClick={close}
      >
        <div
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
          className="w-full max-w-lg rounded-lg border border-outline-warm bg-tertiary-fixed/70 p-3 shadow-hover backdrop-blur-xl dark:shadow-none"
          onClick={(e) => e.stopPropagation()}
        >
          <label htmlFor="palette-input" className="sr-only">
            Run a command
          </label>
          <input
            id="palette-input"
            ref={inputRef}
            type="text"
            value={query}
            role="combobox"
            aria-expanded
            aria-controls="palette-results"
            aria-activedescendant={active ? `palette-${active.id}` : undefined}
            onChange={(e) => {
              setQuery(e.target.value)
              setSelected(0)
            }}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setSelected((i) => Math.min(i + 1, results.length - 1))
              } else if (e.key === 'ArrowUp') {
                e.preventDefault()
                setSelected((i) => Math.max(i - 1, 0))
              } else if (e.key === 'Enter') {
                e.preventDefault()
                runActive()
              }
            }}
            placeholder="Type a command…"
            className="w-full rounded border border-outline bg-surface px-4 py-2 text-body-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
          />

          {results.length === 0 ? (
            <p className="mt-3 px-1 text-caption text-on-surface-variant">
              No command matches “{query}”.
            </p>
          ) : (
            <ul
              id="palette-results"
              role="listbox"
              aria-label="Commands"
              className="mt-3 max-h-80 overflow-y-auto"
            >
              {results.map((command, i) => (
                <li
                  key={command.id}
                  id={`palette-${command.id}`}
                  role="option"
                  aria-selected={i === activeIndex}
                  onMouseEnter={() => setSelected(i)}
                  onClick={() => {
                    close()
                    command.run()
                  }}
                  className={`flex cursor-pointer items-center justify-between gap-3 rounded px-3 py-2 ${
                    i === activeIndex ? 'bg-surface-container-high' : ''
                  }`}
                >
                  <span className="min-w-0 truncate text-body-md text-on-surface">
                    {command.label}
                  </span>
                  <span className="shrink-0 text-caption text-on-surface-variant">
                    {command.hint ?? command.group}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      ) : null}

      {/* Outside the conditional, so it survives the close above. */}
      <PaletteNotice notice={notice} onDismiss={() => setNotice(null)} />
    </>
  )
}
