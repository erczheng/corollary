import { NavLink } from 'react-router-dom'
import { ThemeToggle } from './ThemeToggle'
import { useUIStore } from '../lib/store'
import { ACCOUNT_LABEL } from '../lib/mockData'

const PAGES = [
  { to: '/', label: 'Dashboard' },
  { to: '/activity', label: 'Activity' },
  { to: '/news', label: 'News' },
  { to: '/markets', label: 'Markets' },
  { to: '/research', label: 'Research' },
  { to: '/account', label: 'Account' },
  { to: '/settings', label: 'Settings' },
]

export function Header() {
  const openPalette = useUIStore((s) => s.openPalette)
  const accountMode = useUIStore((s) => s.accountMode)

  return (
    <header className="border-b border-outline-warm bg-surface">
      <div className="flex items-center gap-4 px-4 py-2 lg:px-8">
        <span className="text-title-lg font-bold text-primary">corollary</span>

        <nav className="flex flex-1 items-center gap-1">
          {PAGES.map((page) => (
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
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={openPalette}
            className="rounded border border-outline px-3 py-1.5 text-label-md text-on-surface-variant hover:bg-surface-container-low"
          >
            <kbd className="text-caption">Ctrl K</kbd>
          </button>
          <ThemeToggle />
          <NavLink
            to="/design"
            className="text-caption text-on-surface-variant underline decoration-outline-variant hover:text-on-surface"
          >
            Design
          </NavLink>
        </div>
      </div>
    </header>
  )
}
