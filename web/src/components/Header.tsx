import { NavLink } from 'react-router-dom'
import { ThemeToggle } from './ThemeToggle'
import { useUIStore } from '../lib/store'

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

        {/* The Paper/Cash and Manual/Auto switches live on the Dashboard
            (PRD.md §8.1 "Controls"), not here. */}
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
