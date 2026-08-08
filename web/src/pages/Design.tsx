import type { ReactNode } from 'react'

/* ------------------------------------------------------------------------ *
 * /design — renders every token and component from DESIGN.md so the token
 * system can be verified before any real page depends on it. Nothing here
 * is product UI; it exists to be deleted or graduated into real components
 * once the palette, type scale, and primitives are confirmed correct.
 * ------------------------------------------------------------------------ */

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="mt-20">
      <h2 className="text-title-lg text-on-surface">{title}</h2>
      <div className="mt-3">{children}</div>
    </section>
  )
}

function Swatch({ label, className }: { label: string; className: string }) {
  return (
    <div className="space-y-1">
      <div className={`h-16 rounded-md border border-outline-warm ${className}`} />
      <p className="text-caption text-on-surface-variant">{label}</p>
    </div>
  )
}

const SURFACE_SCALE: { label: string; className: string }[] = [
  { label: 'container-lowest', className: 'bg-surface-container-lowest' },
  { label: 'container-low', className: 'bg-surface-container-low' },
  { label: 'surface (DEFAULT)', className: 'bg-surface' },
  { label: 'container', className: 'bg-surface-container' },
  { label: 'container-high', className: 'bg-surface-container-high' },
  { label: 'container-highest', className: 'bg-surface-container-highest' },
  { label: 'dim', className: 'bg-surface-dim' },
  { label: 'bright', className: 'bg-surface-bright' },
]

const SEMANTIC_GROUPS: {
  label: string
  base: string
  baseText: string
  container: string
  containerText: string
}[] = [
  {
    label: 'Bullish',
    base: 'bg-bullish',
    baseText: 'text-on-bullish',
    container: 'bg-bullish-container',
    containerText: 'text-on-bullish-container',
  },
  {
    label: 'Bearish',
    base: 'bg-bearish',
    baseText: 'text-on-bearish',
    container: 'bg-bearish-container',
    containerText: 'text-on-bearish-container',
  },
  {
    label: 'Neutral',
    base: 'bg-neutral',
    baseText: 'text-on-neutral',
    container: 'bg-neutral-container',
    containerText: 'text-on-neutral-container',
  },
  {
    label: 'Caution',
    base: 'bg-caution',
    baseText: 'text-on-caution',
    container: 'bg-caution-container',
    containerText: 'text-on-caution-container',
  },
]

function SurfaceAndSemanticPreview({ label, dark }: { label: string; dark: boolean }) {
  return (
    <div
      className={
        (dark ? 'dark ' : '') +
        'rounded-lg border border-outline-warm bg-surface p-3 text-on-surface'
      }
    >
      <p className="text-label-md uppercase tracking-wide text-on-surface-variant">
        {label}
      </p>

      <p className="mt-3 text-caption text-on-surface-variant">Surface scale</p>
      <div className="mt-1 grid grid-cols-4 gap-2">
        {SURFACE_SCALE.map((s) => (
          <Swatch key={s.label} label={s.label} className={s.className} />
        ))}
      </div>

      <p className="mt-3 text-caption text-on-surface-variant">Semantic pairs</p>
      <div className="mt-1 grid grid-cols-4 gap-2">
        {SEMANTIC_GROUPS.map((g) => (
          <div key={g.label} className="space-y-1">
            <div
              className={`flex h-10 items-center justify-center rounded-md ${g.base} ${g.baseText} text-label-md`}
            >
              {g.label}
            </div>
            <div
              className={`flex h-10 items-center justify-center rounded-md ${g.container} ${g.containerText} text-caption`}
            >
              container
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

const CORE_COLORS: { label: string; base: string; baseText: string; container?: string; containerText?: string }[] = [
  { label: 'Primary', base: 'bg-primary', baseText: 'text-on-primary', container: 'bg-primary-container', containerText: 'text-on-primary-container' },
  { label: 'Secondary', base: 'bg-secondary', baseText: 'text-on-secondary', container: 'bg-secondary-container', containerText: 'text-on-secondary-container' },
  { label: 'Tertiary', base: 'bg-tertiary', baseText: 'text-on-tertiary', container: 'bg-tertiary-container', containerText: 'text-on-tertiary-container' },
  { label: 'Error', base: 'bg-error', baseText: 'text-on-error', container: 'bg-error-container', containerText: 'text-on-error-container' },
  { label: 'Accent', base: 'bg-accent', baseText: 'text-on-accent', container: 'bg-accent-container', containerText: 'text-on-accent-container' },
]

const FIXED_COLORS: { label: string; base: string; baseText: string; dim: string; dimText: string }[] = [
  { label: 'Primary fixed', base: 'bg-primary-fixed', baseText: 'text-on-primary-fixed', dim: 'bg-primary-fixed-dim', dimText: 'text-on-primary-fixed-variant' },
  { label: 'Secondary fixed', base: 'bg-secondary-fixed', baseText: 'text-on-secondary-fixed', dim: 'bg-secondary-fixed-dim', dimText: 'text-on-secondary-fixed-variant' },
  { label: 'Tertiary fixed', base: 'bg-tertiary-fixed', baseText: 'text-on-tertiary-fixed', dim: 'bg-tertiary-fixed-dim', dimText: 'text-on-tertiary-fixed-variant' },
]

interface Position {
  symbol: string
  contract: string
  qty: number
  last: number
  costBasis: number
  value: number
  pnlUsd: number
  pnlPct: number
}

const POSITIONS: Position[] = [
  { symbol: 'AAPL', contract: '$150 Call Oct 20', qty: 2, last: 182.34, costBasis: 350.0, value: 412.0, pnlUsd: 62.0, pnlPct: 17.71 },
  { symbol: 'TSLA', contract: '$240 Put Nov 15', qty: 1, last: 238.1, costBasis: 410.0, value: 307.5, pnlUsd: -102.5, pnlPct: -25.0 },
  { symbol: 'SPY', contract: '$430/$425 Put Credit Spread', qty: 3, last: 429.88, costBasis: 210.0, value: 168.0, pnlUsd: 42.0, pnlPct: 20.0 },
  { symbol: 'QQQ', contract: '$370 Call Dec 20', qty: 1, last: 372.4, costBasis: 640.0, value: 640.0, pnlUsd: 0, pnlPct: 0 },
]

function formatUsd(value: number): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}$${Math.abs(value).toFixed(2)}`
}

function formatPct(value: number): string {
  const sign = value > 0 ? '+' : value < 0 ? '−' : ''
  return `${sign}${Math.abs(value).toFixed(2)}%`
}

function pnlClass(value: number): string {
  if (value > 0) return 'text-bullish'
  if (value < 0) return 'text-bearish'
  return 'text-on-surface-variant'
}

export function Design() {
  return (
    <div className="mx-auto max-w-[1140px] px-4 py-20 lg:px-16">
      <h1 className="text-display-lg text-on-surface">Design system</h1>
      <p className="mt-3 max-w-prose text-body-md text-on-surface-variant">
        Every token and primitive from DESIGN.md, rendered so the system can
        be checked before a real page depends on it. Use the theme switch in
        the header to confirm the live app; the two panels below force each
        theme independently for direct comparison.
      </p>

      <Section title="Full surface scale &amp; semantic pairs — both themes">
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <SurfaceAndSemanticPreview label="Light" dark={false} />
          <SurfaceAndSemanticPreview label="Dark" dark />
        </div>
      </Section>

      <Section title="Core colors">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {CORE_COLORS.map((c) => (
            <div key={c.label} className="space-y-1">
              <div className={`flex h-12 items-center justify-center rounded-md ${c.base} ${c.baseText} text-label-md`}>
                {c.label}
              </div>
              <div className={`flex h-10 items-center justify-center rounded-md ${c.container} ${c.containerText} text-caption`}>
                container
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Fixed roles (identical in both themes)">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          {FIXED_COLORS.map((c) => (
            <div key={c.label} className="space-y-1">
              <div className={`flex h-10 items-center justify-center rounded-md ${c.base} ${c.baseText} text-caption`}>
                {c.label}
              </div>
              <div className={`flex h-10 items-center justify-center rounded-md ${c.dim} ${c.dimText} text-caption`}>
                dim
              </div>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Outlines (borders only — never text, see DESIGN.md)">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-md border-2 border-outline p-3 text-caption text-on-surface-variant">
            outline — input borders, dense table rules
          </div>
          <div className="rounded-md border-2 border-outline-warm p-3 text-caption text-on-surface-variant">
            outline-warm — cards, chart gridlines
          </div>
          <div className="rounded-md border-2 border-outline-variant p-3 text-caption text-on-surface-variant">
            outline-variant — fine hairlines
          </div>
        </div>
      </Section>

      <Section title="Typography">
        <div className="space-y-3">
          <p className="text-display-lg text-on-surface">Display large</p>
          <p className="text-headline-md text-on-surface">Headline medium</p>
          <p className="text-title-lg text-on-surface">Title large</p>
          <p className="text-body-lg text-on-surface">Body large — the quick brown fox jumps over the lazy dog.</p>
          <p className="text-body-md text-on-surface">Body medium — the quick brown fox jumps over the lazy dog.</p>
          <p className="text-label-md text-on-surface-variant">Label medium — eyebrow header</p>
          <p className="text-caption text-on-surface-variant">Caption — secondary detail text</p>
          <p className="text-data-md text-on-surface">Data medium — $1,234.56 (+2.10%)</p>
          <p className="text-data-lg text-on-surface">Data large — $1,234.56</p>
        </div>
      </Section>

      <Section title="Shapes">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <div className="space-y-1">
            <div className="h-16 rounded-sm bg-primary-container" />
            <p className="text-caption text-on-surface-variant">sm — 0.25rem</p>
          </div>
          <div className="space-y-1">
            <div className="h-16 rounded bg-primary-container" />
            <p className="text-caption text-on-surface-variant">DEFAULT — 0.5rem (buttons, inputs)</p>
          </div>
          <div className="space-y-1">
            <div className="h-16 rounded-md bg-primary-container" />
            <p className="text-caption text-on-surface-variant">md — 0.75rem</p>
          </div>
          <div className="space-y-1">
            <div className="h-16 rounded-lg bg-primary-container" />
            <p className="text-caption text-on-surface-variant">lg — 1rem (cards)</p>
          </div>
          <div className="space-y-1">
            <div className="h-16 rounded-full bg-primary-container" />
            <p className="text-caption text-on-surface-variant">full (chips, pills)</p>
          </div>
        </div>
      </Section>

      <Section title="Buttons">
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" className="rounded bg-primary px-4 py-2 text-label-md text-on-primary">
            Primary action
          </button>
          <button type="button" className="rounded border border-outline px-4 py-2 text-label-md text-on-surface">
            Secondary action
          </button>
          <button type="button" className="rounded border border-error px-4 py-2 text-label-md text-error">
            Flatten
          </button>
          <button type="button" disabled className="rounded border border-outline-warm px-4 py-2 text-label-md text-on-surface-variant opacity-60">
            Disabled
          </button>
        </div>
      </Section>

      <Section title="Inputs">
        <div className="max-w-sm space-y-3">
          <label className="block">
            <span className="text-label-md text-on-surface-variant">Risk per trade (%)</span>
            <input
              type="text"
              placeholder="7.0"
              className="mt-1 w-full rounded border border-outline bg-surface px-3 py-2 text-body-md text-on-surface placeholder:text-on-surface-variant focus:border-primary"
            />
          </label>
          <label className="block">
            <span className="text-label-md text-on-surface-variant">Disabled field</span>
            <input
              type="text"
              disabled
              value="Not editable"
              className="mt-1 w-full rounded border border-outline-warm bg-surface-container-low px-3 py-2 text-body-md text-on-surface-variant"
            />
          </label>
        </div>
      </Section>

      <Section title="Chips &amp; tags">
        <div className="flex flex-wrap gap-2">
          <span className="rounded-full bg-bullish-container px-3 py-1 text-label-md text-on-bullish-container">Bullish</span>
          <span className="rounded-full bg-bearish-container px-3 py-1 text-label-md text-on-bearish-container">Bearish</span>
          <span className="rounded-full bg-neutral-container px-3 py-1 text-label-md text-on-neutral-container">Neutral</span>
          <span className="rounded-full bg-caution-container px-3 py-1 text-label-md text-on-caution-container">Pending</span>
          <span className="rounded-full bg-accent-container px-3 py-1 text-label-md text-on-accent-container">NEW</span>
          <span className="rounded-full bg-accent-container px-3 py-1 text-label-md text-on-accent-container">Unvalidated</span>
        </div>
      </Section>

      <Section title="Dense table — tabular numerals">
        <div className="overflow-hidden rounded-lg border border-outline-warm">
          <table className="w-full border-collapse">
            <thead>
              <tr className="bg-surface-container">
                <th className="px-4 py-3 text-left text-label-md uppercase text-on-surface-variant">Symbol</th>
                <th className="px-4 py-3 text-left text-label-md uppercase text-on-surface-variant">Contract</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">Qty</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">Last</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">Cost basis</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">Value</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">P&amp;L ($)</th>
                <th className="px-4 py-3 text-right text-label-md uppercase text-on-surface-variant">P&amp;L (%)</th>
              </tr>
            </thead>
            <tbody>
              {POSITIONS.map((p) => (
                <tr key={p.symbol} className="border-t border-outline/10 hover:bg-surface-container-low">
                  <td className="px-4 py-3 text-body-md text-on-surface">{p.symbol}</td>
                  <td className="px-4 py-3 text-body-md text-on-surface-variant">{p.contract}</td>
                  <td className="px-4 py-3 text-right text-data-md text-on-surface">{p.qty}</td>
                  <td className="px-4 py-3 text-right text-data-md text-on-surface">${p.last.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right text-data-md text-on-surface">${p.costBasis.toFixed(2)}</td>
                  <td className="px-4 py-3 text-right text-data-md text-on-surface">${p.value.toFixed(2)}</td>
                  <td className={`px-4 py-3 text-right text-data-md ${pnlClass(p.pnlUsd)}`}>{formatUsd(p.pnlUsd)}</td>
                  <td className={`px-4 py-3 text-right text-data-md ${pnlClass(p.pnlPct)}`}>{formatPct(p.pnlPct)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  )
}
