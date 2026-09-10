import type { ReactNode } from 'react'
import type { Sentiment } from '../lib/mockData'

export type ChipVariant = 'bullish' | 'bearish' | 'neutral' | 'caution' | 'accent'

const VARIANT_CLASSES: Record<ChipVariant, string> = {
  bullish: 'bg-bullish-container text-on-bullish-container',
  bearish: 'bg-bearish-container text-on-bearish-container',
  neutral: 'bg-neutral-container text-on-neutral-container',
  caution: 'bg-caution-container text-on-caution-container',
  accent: 'bg-accent-container text-on-accent-container',
}

export function Chip({
  variant,
  title,
  children,
}: {
  variant: ChipVariant
  /** Hover text. A chip is often an abbreviation of something longer — the
   * full meaning has to be reachable without leaving the row. */
  title?: string
  children: ReactNode
}) {
  return (
    <span
      title={title}
      className={`inline-flex h-6 items-center rounded-full px-2 text-label-sm ${VARIANT_CLASSES[variant]}`}
    >
      {children}
    </span>
  )
}

const SENTIMENT_VARIANT: Record<Sentiment, ChipVariant> = {
  bullish: 'bullish',
  bearish: 'bearish',
  neutral: 'neutral',
  unclassified: 'caution',
}

const SENTIMENT_LABEL: Record<Sentiment, string> = {
  bullish: 'Bullish',
  bearish: 'Bearish',
  neutral: 'Neutral',
  unclassified: 'Unclassified',
}

/** Sentiment gates below the confidence threshold publish as
 * "Unclassified" rather than a wrong label (PRD.md §9) — the caution
 * variant reads as "pending," never as an error. */
export function SentimentChip({ sentiment }: { sentiment: Sentiment }) {
  return <Chip variant={SENTIMENT_VARIANT[sentiment]}>{SENTIMENT_LABEL[sentiment]}</Chip>
}
