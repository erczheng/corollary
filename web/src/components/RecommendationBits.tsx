import { Chip } from './Chip'
import { XIcon } from './icons'
import { CONFIDENCE_TIER_CLASS, confidenceTier } from '../lib/format'
import { recommendationTitle } from '../lib/mockData'
import { type Recommendation } from '../lib/types'
import type { Disposition } from '../lib/research'

/** The parts of a recommendation row that both the Dashboard panel and
 * Research's full table render.
 *
 * **Deliberately atoms rather than one table with two layouts**, which is how
 * `ExecutionsTable` handles the same problem. The difference is that the two
 * executions views are both tables differing only in column set, whereas these
 * two are different shapes: the Dashboard shows a compact two-line card list
 * sized for a half-width panel, and Research shows a wide table with setup,
 * reason, origin and disposition columns. Forcing one component to be both
 * would need a prop for every structural difference, which is a worse outcome
 * than sharing the pieces that can actually drift.
 *
 * What can drift is exactly this: the confidence slot has three branches that
 * are easy to get subtly wrong, and the action set has to agree about what a
 * halted engine permits.
 */

/** The confidence slot, and all three of its states.
 *
 * `confidence` is the **backtested hit rate for the setup class** (PRD.md
 * §6.3), not a model's self-report — hence the tooltip, because a bare
 * percentage next to an LLM-adjacent feature reads as model confidence.
 *
 * Null splits two ways and they are different facts:
 * - an unvalidated origination has no base rate *by construction* (§6.2 — no
 *   setup matched), so it says so and mentions the ⅓ size cap;
 * - a scanner candidate whose setup class simply has too few samples yet gets
 *   the em dash §6.3 asks for.
 *
 * Collapsing those into one em dash would lose the reason, and showing a
 * number for either would invent a base rate that does not exist.
 *
 * Colour comes from `CONFIDENCE_TIER_CLASS` — `primary`/`caution`/`neutral`,
 * never `bullish`/`bearish`. A high-confidence bearish trade is ordinary here,
 * and a green 71% beside a put spread reads as direction rather than
 * conviction. */
export function ConfidenceBadge({ recommendation }: { recommendation: Recommendation }) {
  const { confidence, unvalidated } = recommendation

  if (confidence !== null) {
    return (
      <span
        className={`inline-flex h-6 items-center rounded-full px-2 text-data-sm ${CONFIDENCE_TIER_CLASS[confidenceTier(confidence)]}`}
        title={`${confidenceTier(confidence)} confidence — backtested hit rate for this setup class`}
      >
        {confidence}%
      </span>
    )
  }

  if (unvalidated) {
    return (
      <Chip
        variant="accent"
        title="Unvalidated — no setup match, so no backtested base rate. Capped at ⅓ normal size."
      >
        Untested
      </Chip>
    )
  }

  return (
    <span
      className="inline-flex h-6 items-center px-2 text-data-sm text-on-surface-variant"
      title="No backtested base rate for this setup yet"
    >
      —
    </span>
  )
}

const DISPOSITION_LABEL: Record<Exclude<Disposition, 'open'>, string> = {
  executed: 'Executed',
  dismissed: 'Dismissed',
}

const DISPOSITION_TITLE: Record<Exclude<Disposition, 'open'>, string> = {
  executed: 'Submitted to the risk manager. Manual execution from this list lands in Phase 6.',
  dismissed: 'Waved off. A refresh restores it — the scanner rebuilds its candidate set.',
}

/** What has been done with a row.
 *
 * `neutral`, not `bullish` — acting on a recommendation is not a win, and
 * colouring an executed row green would be reporting an outcome it does not
 * have yet. */
export function DispositionBadge({ disposition }: { disposition: Disposition }) {
  if (disposition === 'open') return null
  return (
    <Chip variant="neutral" title={DISPOSITION_TITLE[disposition]}>
      {DISPOSITION_LABEL[disposition]}
    </Chip>
  )
}

/** Execute / Dismiss.
 *
 * Two actions, not three. `Queue` sat between them — staged for the engine's
 * next run, nothing sent to a broker — and was removed deliberately, so
 * accepting a candidate now means submitting it and nothing else.
 *
 * `onExecute` opens the caller's confirm rather than placing anything itself —
 * neither view submits an order from a click, and in Phase 6 that path runs
 * through `RiskManager.approve()`.
 *
 * Execute is disabled while the engine is halted: halting stops new entries.
 * Dismiss stays enabled — waving off a candidate is not opening a position,
 * and there is no reason a halt should stop you tidying the list.
 *
 * `compact` shortens Execute to "Trade" for the Dashboard's half-width panel,
 * which is the wording §8.1 uses. */
export function RecommendationActions({
  recommendation,
  disposition,
  halted,
  compact = false,
  onExecute,
  onDismiss,
}: {
  recommendation: Recommendation
  disposition: Disposition
  halted: boolean
  compact?: boolean
  onExecute: () => void
  onDismiss: () => void
}) {
  const title = recommendationTitle(recommendation)
  const haltedTitle = 'Trading is halted — resume to open new positions'
  const acted = disposition === 'executed'

  const button =
    'inline-flex h-6 items-center rounded border px-2 text-label-sm transition-colors duration-base ease-standard disabled:pointer-events-none disabled:border-outline-warm disabled:text-on-surface-variant disabled:opacity-50'

  return (
    <div className="flex shrink-0 items-center gap-2">
      <button
        type="button"
        onClick={onExecute}
        disabled={halted || acted}
        title={halted ? haltedTitle : acted ? 'Already acted on' : `Trade ${title}`}
        className={`${button} border-primary text-primary hover:bg-primary-container hover:text-on-primary-container`}
      >
        {compact ? 'Trade' : 'Execute'}
      </button>

      <button
        type="button"
        onClick={onDismiss}
        aria-label={`Dismiss ${title}`}
        title="Dismiss"
        className="flex h-6 w-6 items-center justify-center rounded-full text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-high hover:text-on-surface"
      >
        <XIcon className="h-3.5 w-3.5" />
      </button>
    </div>
  )
}
