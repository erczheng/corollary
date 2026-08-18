import { useEffect, useId, useRef, useState } from 'react'
import { searchUnderlyings } from '../lib/markets'

/** Type-to-search symbol picker.
 *
 * A native `<select>` is fine at six underlyings and useless at six hundred,
 * which is what Phase 2's asset list is — so the control that survives the
 * data source change is the one that filters. It stays a combobox rather
 * than a free-text field because the value has to *be* a listed symbol:
 * typing "APPL" should find nothing rather than filtering a chain to
 * emptiness and implying the contracts stopped trading.
 *
 * Keyboard-complete on purpose. This is the primary control on the page and
 * the one most likely to be driven without a mouse: Arrow keys move the
 * active option, Enter commits it, Escape reverts to what was selected. The
 * roving state is `aria-activedescendant` rather than real focus, which is
 * what keeps the typed query and the highlighted option in the same place.
 */
export function SymbolCombobox({
  symbols,
  value,
  onChange,
  label,
  allLabel = 'All underlyings',
}: {
  symbols: string[]
  /** The selected symbol, or null for "all". */
  value: string | null
  onChange: (value: string | null) => void
  label: string
  allLabel?: string
}) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const wrapper = useRef<HTMLDivElement>(null)

  // `null` is a real option, not the absence of one, so it sits at the head
  // of the list and is reachable by the same keys as everything else.
  const matches = searchUnderlyings(symbols, query)
  const options: (string | null)[] = query.trim() === '' ? [null, ...matches] : matches

  const display = value ?? allLabel

  useEffect(() => {
    if (!open) return
    function onPointerDown(e: MouseEvent) {
      if (!wrapper.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  function commit(option: string | null) {
    onChange(option)
    setQuery('')
    setOpen(false)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      if (!open) {
        setOpen(true)
        setActive(0)
        return
      }
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((i) => (options.length === 0 ? 0 : (i + step + options.length) % options.length))
      return
    }

    if (e.key === 'Enter') {
      if (!open || options.length === 0) return
      e.preventDefault()
      commit(options[Math.min(active, options.length - 1)])
      return
    }

    if (e.key === 'Escape') {
      // Back to the committed value, not to empty — Escape cancels the
      // search rather than clearing the selection behind it.
      setQuery('')
      setOpen(false)
    }
  }

  return (
    <div ref={wrapper} className="relative">
      <input
        id={id}
        type="text"
        role="combobox"
        aria-expanded={open}
        aria-controls={`${id}-listbox`}
        aria-autocomplete="list"
        aria-activedescendant={open && options.length > 0 ? `${id}-option-${active}` : undefined}
        aria-label={label}
        // The committed symbol shows as the placeholder once you start
        // typing, so the field never looks empty of a selection it still
        // holds.
        placeholder={display}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value)
          setActive(0)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        className={`w-40 rounded border border-outline bg-surface px-2 py-2 text-label-md focus:border-primary ${
          query === '' ? 'text-on-surface placeholder:text-on-surface' : 'text-on-surface'
        }`}
      />

      {open && (
        <ul
          id={`${id}-listbox`}
          role="listbox"
          aria-label={label}
          /* max-h-96, not max-h-64: seven options measure 260px and the
             tighter cap clipped them at 254, so the list scrolled by six
             pixels and showed a bar implying more below. The cap is still
             here — Phase 2's asset list is hundreds of symbols — it just
             sits above the list that actually exists. overflow-y rather
             than overflow, so a 40px-wide list can never grow a horizontal
             bar too. */
          className="absolute right-0 z-20 mt-1 max-h-96 w-40 overflow-y-auto rounded border border-outline-warm bg-surface-container-lowest py-1"
        >
          {options.length === 0 ? (
            <li className="px-3 py-2 text-caption text-on-surface-variant">
              No listed chain matches “{query.trim()}”.
            </li>
          ) : (
            options.map((option, i) => {
              const selected = option === value
              return (
                <li
                  key={option ?? '__all'}
                  id={`${id}-option-${i}`}
                  role="option"
                  aria-selected={selected}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => {
                    // Commit before the input loses focus, or the outside
                    // click closes the list first and swallows the choice.
                    e.preventDefault()
                    commit(option)
                  }}
                  className={`cursor-pointer px-3 py-2 text-label-md ${
                    i === active ? 'bg-surface-container text-on-surface' : 'text-on-surface-variant'
                  } ${selected ? 'font-semibold text-primary' : ''}`}
                >
                  {option ?? allLabel}
                </li>
              )
            })
          )}
        </ul>
      )}
    </div>
  )
}
