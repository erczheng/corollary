import type { ReactNode } from 'react'

/** A section label in the News page's main column — small, uppercase, no
 * card around it. The feed and the calendar are the page's subject rather
 * than panels on it, so boxing them would add a border for nothing; the
 * rail's cards are what they sit beside.
 *
 * Its own file because the market calendar moved out of `News.tsx` into a
 * component of its own, and two copies of a heading drift. */
export function SectionLabel({
  id,
  children,
  aside,
}: {
  id: string
  children: ReactNode
  aside?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm pb-2">
      <h2 id={id} className="text-label-md uppercase tracking-wide text-on-surface-variant">
        {children}
      </h2>
      {aside}
    </div>
  )
}
