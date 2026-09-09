import type { ReactNode } from 'react'

/** One card on the Settings page.
 *
 * A `region` with its heading as the accessible name, so the seven sections
 * are individually addressable — by a screen reader, and by a test that would
 * otherwise be matching "Bell" or "Status" across three different tables.
 *
 * `description` is not decoration. Several of these settings are
 * uninterpretable without one sentence of context — a percentage ceiling that
 * doesn't say what it measures, or a feed name that doesn't say what fraction
 * of the market it covers — and putting that sentence anywhere other than
 * beside the control means it isn't read. */
export function SettingsSection({
  id,
  title,
  description,
  aside,
  children,
}: {
  id: string
  title: string
  description?: ReactNode
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <section
      aria-labelledby={id}
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id={id} className="text-title-lg text-on-surface">
            {title}
          </h2>
          {description ? (
            <p className="mt-1 max-w-prose text-caption text-on-surface-variant">{description}</p>
          ) : null}
        </div>
        {aside ? <div className="shrink-0">{aside}</div> : null}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  )
}
