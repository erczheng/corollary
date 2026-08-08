import { Link, NavLink } from 'react-router-dom'
import { ThemeToggle } from './ThemeToggle'
import { BellIcon, GearIcon, UserIcon } from './icons'
import { useUIStore } from '../lib/store'
import { ACCOUNT_LABEL } from '../lib/mockData'

/** The five pages you move between while trading. They keep their words:
 * these are destinations you pick deliberately, and a row of five unlabeled
 * glyphs is a guessing game. */
const NAV_PAGES = [
  { to: '/', label: 'Dashboard' },
  { to: '/activity', label: 'Activity' },
  { to: '/news', label: 'News' },
  { to: '/markets', label: 'Markets' },
  { to: '/research', label: 'Research' },
]

/** Icon controls in the right-hand cluster share one shape so the row reads
 * as a set. */
const ICON_BUTTON =
  'flex h-9 w-9 items-center justify-center rounded transition-colors duration-base ease-standard'

const ICON_IDLE = 'text-on-surface-variant hover:bg-surface-container-low hover:text-on-surface'

/** Account and Settings are utilities rather than destinations — visited
 * occasionally and by intent — so they join the tool cluster on the right
 * as icons instead of taking a word each in the main nav. Both glyphs are
 * conventional enough (person, gear) to carry the meaning unlabeled, which
 * would not be true of Markets or Research.
 *
 * The word survives as `aria-label` and `title`: an icon link with no
 * accessible name is announced as nothing at all. `h-4 w-4` is explicit
 * because a custom className on these icon components *replaces* their
 * default sizing rather than merging with it (CLAUDE.md). */
function IconNavLink({
  to,
  label,
  children,
}: {
  to: string
  label: string
  children: React.ReactNode
}) {
  return (
    <NavLink
      to={to}
      aria-label={label}
      title={label}
      className={({ isActive }) => `${ICON_BUTTON} ${isActive ? 'text-primary' : ICON_IDLE}`}
    >
      {children}
    </NavLink>
  )
}

export function Header() {
  const openPalette = useUIStore((s) => s.openPalette)
  const accountMode = useUIStore((s) => s.accountMode)

  return (
    <header className="border-b border-outline-warm bg-surface">
      {/* The bar itself stays full-bleed so its bottom rule spans the window,
          but its contents ride the same 1425px container and the same 48px
          side padding every page uses. Without this the wordmark sat hard
          against the window edge while the page title below it started
          ~100px in, and the two never lined up. Any change to the page
          container has to be mirrored here or they drift apart again. */}
      <div className="mx-auto flex max-w-[1425px] items-center gap-4 px-4 py-2 lg:px-12">
        {/* The wordmark is the way home, the way it is on every other site.
            A plain Link, not a NavLink: it carries no active styling, and a
            NavLink would mark it aria-current="page" on the Dashboard —
            a second "current page" competing with the real nav item below.
            Focus ring comes from the global :focus-visible rule in
            index.css; `rounded` just keeps that ring off the glyphs.

            Hover goes to `on-surface`, the same as the "View all" links —
            not `primary-container`, which is lighter than `primary` in
            light theme but far darker in dark, so it would sink the
            wordmark into `#13140d` on the one theme where it needs the
            contrast most. */}
        <Link
          to="/"
          aria-label="corollary — go to Dashboard"
          className="rounded text-title-lg font-bold text-primary transition-colors duration-base ease-standard hover:text-on-surface"
        >
          corollary
        </Link>

        <nav aria-label="Main" className="flex flex-1 items-center gap-1">
          {NAV_PAGES.map((page) => (
            <NavLink
              key={page.to}
              to={page.to}
              end={page.to === '/'}
              className={({ isActive }) =>
                isActive
                  ? 'rounded px-3 py-2 text-label-md text-primary'
                  : 'rounded px-3 py-2 text-label-md text-on-surface-variant hover:text-on-surface'
              }
            >
              {page.label}
            </NavLink>
          ))}
        </nav>

        {/* The Paper/Cash and Manual/Auto *switches* live on the Dashboard
            (PRD.md §8.1 "Controls"), not here. This is the read-only badge
            PRD.md §3 names as the mitigation for that: Activity, Markets,
            Account and the rest show account-scoped money with no switch on
            screen, so something has to say which book you're looking at.
            A pill, not a button — it reports, it doesn't change anything.

            Cash takes `caution`, not `error`: real money in play is a
            reason to read carefully, not a failure. Paper recedes into
            `neutral` because it's the default and the safe one. */}
        <span
          aria-label={`Account: ${ACCOUNT_LABEL[accountMode]}`}
          title={
            accountMode === 'cash'
              ? 'Live Cash account — orders here use real money. Switch on the Dashboard.'
              : 'Paper account — no real money at risk. Switch on the Dashboard.'
          }
          className={`rounded-full px-3 py-1 text-label-md ${
            accountMode === 'cash'
              ? 'bg-caution-container text-on-caution-container'
              : 'bg-neutral-container text-on-neutral-container'
          }`}
        >
          {ACCOUNT_LABEL[accountMode]}
        </span>
        {/* Search, then the utility icons, then the theme toggle. Account and
            Settings sit between the two so the cluster runs from "find
            something" through "your stuff" to "how it looks". */}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={openPalette}
            aria-label="Search"
            className="mr-1 rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
          >
            <kbd className="text-caption">Ctrl K</kbd>
          </button>

          <IconNavLink to="/account" label="Account">
            <UserIcon className="h-4 w-4" />
          </IconNavLink>

          {/* Notifications has no feed behind it yet — the events are routed
              in Settings, and the bell that receives them lands with them.
              Deliberately badge-free until then: an unread count that
              nothing can produce is a decoration that lies. */}
          <button
            type="button"
            aria-label="Notifications"
            title="Notifications — no unread"
            className={`${ICON_BUTTON} ${ICON_IDLE}`}
          >
            <BellIcon className="h-4 w-4" />
          </button>

          <IconNavLink to="/settings" label="Settings">
            <GearIcon className="h-4 w-4" />
          </IconNavLink>

          <ThemeToggle />
          <NavLink
            to="/design"
            className="ml-1 text-caption text-on-surface-variant underline decoration-outline-variant hover:text-on-surface"
          >
            Design
          </NavLink>
        </div>
      </div>
    </header>
  )
}
