import { Link, useLocation } from 'react-router-dom'
import { DESTINATIONS } from '../lib/routes'

/** The catch-all route.
 *
 * Without one, an unknown URL rendered the header, the account badge and
 * the notification bell over a completely empty `<main>` — the chrome all
 * present and working, nothing beneath it. On a terminal that is otherwise
 * always showing numbers, that reads as a crash rather than as a wrong
 * address, which is the single worst thing a blank screen can mean here.
 *
 * So the page says three things, in this order: nothing lives at that
 * address, **the engine is unaffected**, and here is everywhere that does
 * exist. The middle one is the reason this page is worth designing at all.
 *
 * Neutral, not `error`. CLAUDE.md keeps `error` for rule outcomes and
 * system faults — a rejected order, a dead connection — and mistyping a URL
 * is neither. Painting this red would say the terminal is broken at the
 * exact moment the useful message is that it isn't.
 */
export function NotFound() {
  const { pathname } = useLocation()

  return (
    <div className="mx-auto max-w-4xl px-4 py-12 lg:px-12">
      <h1 className="text-display-lg text-on-surface">No page at this address</h1>

      <p className="mt-4 max-w-prose text-body-md text-on-surface-variant">
        Corollary has no route for{' '}
        <code className="rounded bg-surface-container px-1.5 py-0.5 text-data-md text-on-surface">
          {pathname}
        </code>
      </p>

      {/* The reassurance is the point of the page, so it gets its own block
          rather than a clause at the end of a paragraph. */}
      <p className="mt-3 max-w-prose text-body-md text-on-surface-variant">
        This is a navigation miss, not an engine fault. Nothing has changed about your positions,
        your working orders, or whether the engine is halted.
      </p>

      <h2 className="mt-10 text-caption uppercase tracking-wide text-on-surface-variant">
        Where you can go
      </h2>

      <ul className="mt-3 divide-y divide-outline-variant rounded-lg border border-outline-warm bg-surface-container-lowest">
        {DESTINATIONS.map((page) => (
          <li key={page.to}>
            <Link
              to={page.to}
              className="flex flex-col gap-0.5 px-4 py-3 transition-colors duration-base ease-standard hover:bg-surface-container-low"
            >
              <span className="text-label-md text-primary">{page.label}</span>
              <span className="text-caption text-on-surface-variant">{page.blurb}</span>
            </Link>
          </li>
        ))}
      </ul>

      <p className="mt-6 text-caption text-on-surface-variant">
        Ctrl K opens the command palette from anywhere, including here.
      </p>
    </div>
  )
}
