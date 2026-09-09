import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ConfirmDialog } from './ConfirmDialog'
import { useUIStore } from '../lib/store'
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

/** Ctrl+K palette — PRD.md §11 names it in the Phase 1 bar.
 *
 * Three groups, which is what the placeholder here promised: jump to a page,
 * act on a recommendation, control the engine.
 *
 * **Flatten keeps its confirm.** It is reachable by typing three letters and
 * pressing Enter, which makes it the single easiest destructive action in the
 * app to trigger by accident — a fuzzy-matched Enter with no confirm is how you
 * close the whole book while meaning to jump to a page. Halt and Flatten stay
 * separate entries for the same reason they are separate functions everywhere
 * else (CLAUDE.md rule 7): one stops new entries, the other closes everything.
 */
export function CommandPalette() {
  const open = useUIStore((s) => s.paletteOpen)
  const close = useUIStore((s) => s.closePalette)
  const navigate = useNavigate()

  const isHalted = useUIStore((s) => s.isHalted)
  const halt = useUIStore((s) => s.halt)
  const resume = useUIStore((s) => s.resume)
  const flatten = useUIStore((s) => s.flatten)
  const dispositions = useUIStore((s) => s.dispositions)
  const executeRecommendation = useUIStore((s) => s.executeRecommendation)
  const queueRecommendation = useUIStore((s) => s.queueRecommendation)
  const dismissRecommendation = useUIStore((s) => s.dismissRecommendation)

  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const [confirmingFlatten, setConfirmingFlatten] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

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
      // Execute *and* Queue, gated together on the halt.
      //
      // Both, because PRD.md §8.5 treats them as peers and the distinction
      // between them is load-bearing: executed means an order went to the
      // risk manager, queued means the engine will place one on its next
      // run and nothing has been sent yet. Offering only Execute here made
      // the palette's one order action the irreversible one, while the
      // safer half of the pair was reachable only from Research.
      //
      // Gated, because §8.5 disables both while the engine is halted and
      // the palette was not honouring that — halting stops new entries
      // (CLAUDE.md rule 7), and a surface that still offers to open one is
      // the surface that will be used to do it by accident. Omitted rather
      // than shown-disabled: there is no disabled state in a command list,
      // and a command that silently does nothing is worse than an absent
      // one.
      if (!acted && !isHalted) {
        list.push(
          {
            id: `exec-${r.id}`,
            label: `Execute ${title}`,
            group: 'Recommendations',
            hint: 'Submits to the risk manager',
            run: () => executeRecommendation(r.id),
          },
          {
            id: `queue-${r.id}`,
            label: `Queue ${title}`,
            group: 'Recommendations',
            hint: 'Staged for the engine’s next run — nothing sent yet',
            run: () => queueRecommendation(r.id),
          },
        )
      }
      // Never gated on the halt: waving off a candidate opens nothing.
      list.push({
        id: `dismiss-${r.id}`,
        label: `Dismiss ${title}`,
        group: 'Recommendations',
        run: () => dismissRecommendation(r.id),
      })
    }

    list.push(
      isHalted
        ? {
            id: 'resume',
            label: 'Resume trading',
            group: 'Engine',
            hint: 'Allows new entries again',
            run: resume,
          }
        : {
            id: 'halt',
            label: 'Halt trading',
            group: 'Engine',
            hint: 'Stops new entries — open positions keep their exits',
            run: halt,
          },
      {
        id: 'flatten',
        label: 'Flatten all positions',
        group: 'Engine',
        hint: 'Closes everything, then halts — asks first',
        run: () => setConfirmingFlatten(true),
      },
    )

    return list
  }, [
    navigate,
    dispositions,
    executeRecommendation,
    queueRecommendation,
    dismissRecommendation,
    isHalted,
    halt,
    resume,
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
      {/* The palette body is conditional; the confirm below is NOT.
          `if (!open) return null` used to sit above this return, which meant
          running Flatten — whose first act is to close the palette — unmounted
          the confirm dialog in the same tick it was asked for. The dialog could
          never appear, so the safest-looking command in the list was the one
          with no guard behind it. */}
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
      <ConfirmDialog
        open={confirmingFlatten}
        title="Flatten all positions?"
        consequence="Closes every open position in this account at the current bid or ask, then halts the engine. Managed exits go with them. This cannot be undone."
        confirmLabel="Flatten and halt"
        destructive
        onConfirm={() => {
          flatten()
          setConfirmingFlatten(false)
        }}
        onCancel={() => setConfirmingFlatten(false)}
      />
    </>
  )
}
