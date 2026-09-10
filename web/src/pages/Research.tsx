import { useState } from 'react'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { RefreshButton } from '../components/RefreshButton'
import { ResearchChat } from '../components/ResearchChat'
import { StatCard } from '../components/StatCard'
import { StrategyList } from '../components/StrategyList'
import {
  ConfidenceBadge,
  DispositionBadge,
  RecommendationActions,
} from '../components/RecommendationBits'
import { Chip } from '../components/Chip'
import { TargetIcon, TrendingUpIcon } from '../components/icons'
import { useUIStore } from '../lib/store'
import {
  LLM_ORIGINATION,
  MARKET_PULSE,
  RECOMMENDATIONS,
  SENTIMENT_COMPONENTS,
  recommendationTitle,
  type Recommendation,
} from '../lib/mockData'
import { compositeScore } from '../lib/news'
import { dispositionOf, recommendationCsvRows, visibleRecommendations } from '../lib/research'
import { downloadCsv } from '../lib/csv'
import { formatExpiry, formatSignedNumber, formatUsd, signClass } from '../lib/format'

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'

/** PRD.md §8.5. The page for deciding what to trade and which strategy should
 * be running, as opposed to the Dashboard's "what is my state right now".
 *
 * The LLM origination panel sits with the strategy list, as §8.5 asks, and is
 * deliberately **absent from the Dashboard** — keeping it here is what stops
 * the headline win rate being quietly diluted by a separate bucket of trades
 * with a different risk profile.
 */
export function Research() {
  const dispositions = useUIStore((s) => s.dispositions)
  const executeRecommendation = useUIStore((s) => s.executeRecommendation)
  const dismissRecommendation = useUIStore((s) => s.dismissRecommendation)
  const refreshRecommendations = useUIStore((s) => s.refreshRecommendations)
  const isHalted = useUIStore((s) => s.isHalted)
  const executionMode = useUIStore((s) => s.executionMode)

  const [tradeTarget, setTradeTarget] = useState<Recommendation | null>(null)

  const visible = visibleRecommendations(RECOMMENDATIONS, dispositions)
  const engineHalted = executionMode === 'auto' && isHalted

  // Derived from the components rather than stored, so this can never disagree
  // with the breakdown the News page prints underneath the same number.
  const composite = compositeScore(SENTIMENT_COMPONENTS)

  // Day's change on the VIX, derived from the previous close so the level
  // and the move come from one number rather than two that could drift.
  const vixChange = MARKET_PULSE.vix - MARKET_PULSE.vixPreviousClose
  const vixChangePct = (vixChange / MARKET_PULSE.vixPreviousClose) * 100

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <h1 className="text-display-lg text-on-surface">Research</h1>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Today’s full candidate set, the strategies behind it, and how the LLM layer is earning its
        place. The chat is scoped to this account and its data.
      </p>

      {/* Market Pulse (§8.5). VIX carries the day's change; the other two do
          not, because neither has a baseline on this page to compare against
          and StatCard's own note says inventing one is worse than showing
          none. */}
      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* Derived from the previous close rather than read from a stored
            change, so the level and the move cannot disagree.

            `neutral` tone: a rising VIX is not a gain. Colouring it green
            would report risk-off as good news, and the fixture closes
            *down* precisely so that branch is the one on screen.

            The change displaces the "implied 30-day volatility" note —
            StatCard shows one line or the other, and a second would make
            this card taller than the two beside it. Today's move is the
            more useful of the two on a page you read each morning. */}
        <StatCard
          label="VIX"
          value={MARKET_PULSE.vix.toFixed(2)}
          icon={<TrendingUpIcon />}
          changePct={vixChangePct}
          trendTone="neutral"
          comparedTo={`${formatSignedNumber(vixChange)} pts vs previous close`}
        />
        {/* The note is kept to one line at every three-column width. That
            slot is ~294px wide here and only ~266px at the `lg` breakpoint,
            where the cards are narrowest; the full sentence wanted 394px, so
            it wrapped and made this the one card in the row two lines taller
            than the other two. */}
        <StatCard
          label="Sentiment composite"
          value={`${composite}`}
          icon={<TargetIcon />}
          note="0–100 · seven components · News"
        />
        <StatCard
          label="Top sector"
          value={MARKET_PULSE.topSector}
          icon={<TrendingUpIcon />}
          note="Leading sector this session"
        />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[1.6fr_1fr]">
        <ResearchChat />

        <div className="flex flex-col gap-4">
          <OriginationPanel />
          <StrategyList />
        </div>
      </div>

      <section
        aria-labelledby="candidates-heading"
        className="mt-4 rounded-lg border border-outline-warm bg-surface-container-lowest"
      >
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-warm px-4 py-3">
          <h2 id="candidates-heading" className="text-title-lg text-on-surface">
            Recommended Trades
          </h2>
          <div className="flex items-center gap-2">
            <RefreshButton onRefresh={refreshRecommendations} />
            {/* Exports the whole candidate set, dismissed rows included — the
                CSV is a record of what the scanner produced this session, not
                a copy of what happens to be on screen. */}
            <button
              type="button"
              onClick={() =>
                downloadCsv('recommendations.csv', recommendationCsvRows(RECOMMENDATIONS, dispositions))
              }
              className="rounded border border-outline px-3 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
            >
              Export CSV
            </button>
          </div>
        </div>

        {visible.length === 0 ? (
          <p className="px-4 py-6 text-body-md text-on-surface-variant">
            Every candidate has been dismissed. Refresh to bring them back — the scanner rebuilds its
            set and does not remember what you waved off.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <caption className="sr-only">Today’s scanner and LLM candidates</caption>
              <thead>
                <tr className="border-b border-outline">
                  <th scope="col" className={`${TH} text-left`}>
                    Contract
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Expiry
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Setup
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Why
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Origin
                  </th>
                  <th scope="col" className={`${TH} text-right`}>
                    Confidence
                  </th>
                  <th scope="col" className={`${TH} text-right`}>
                    Action
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => {
                  const disposition = dispositionOf(r.id, dispositions)
                  return (
                    <tr key={r.id} className="h-8 border-b border-outline-variant last:border-0">
                      <td className={`${TD} text-body-sm text-on-surface`}>
                        {recommendationTitle(r)}
                      </td>
                      {/* Formatted in UTC. A bare YYYY-MM-DD parses as UTC
                          midnight, and rendering it in ET shows the previous
                          day — a Nov 21 expiry displaying as Nov 20. */}
                      <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                        {formatExpiry(r.expiry)}
                      </td>
                      <td className={`${TD} text-caption text-on-surface-variant`}>{r.setup}</td>
                      <td className={`${TD} text-caption text-on-surface-variant`}>{r.reason}</td>
                      <td className={TD}>
                        <div className="flex items-center">
                          {r.origin === 'llm' ? (
                            <Chip variant="accent" title="Originated by the LLM layer, not the scanner">
                              LLM
                            </Chip>
                          ) : (
                            <span className="text-caption text-on-surface-variant">Scanner</span>
                          )}
                        </div>
                      </td>
                      <td className={`${TD} text-right`}>
                        <div className="flex items-center justify-end">
                          <ConfidenceBadge recommendation={r} />
                        </div>
                      </td>
                      <td className={`${TD} text-right`}>
                        <div className="flex items-center justify-end gap-2">
                          <DispositionBadge disposition={disposition} />
                          <RecommendationActions
                            recommendation={r}
                            disposition={disposition}
                            halted={engineHalted}
                            onExecute={() => setTradeTarget(r)}
                            onDismiss={() => dismissRecommendation(r.id)}
                          />
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <ConfirmDialog
        open={tradeTarget !== null}
        title="Submit this trade?"
        consequence={
          tradeTarget && (
            <>
              Submits {recommendationTitle(tradeTarget)}, expiring {formatExpiry(tradeTarget.expiry)},
              to the risk manager. It is approved or rejected server-side against every limit —
              nothing here can override one.
              {tradeTarget.unvalidated
                ? ' This is an unvalidated origination: no setup matched, so it is capped at a third of normal size.'
                : ''}
            </>
          )
        }
        confirmLabel="Submit to risk manager"
        onConfirm={() => {
          if (tradeTarget) executeRecommendation(tradeTarget.id)
          setTradeTarget(null)
        }}
        onCancel={() => setTradeTarget(null)}
      />
    </div>
  )
}

/** Is the LLM layer earning its place? (PRD.md §8.5, §6.4.)
 *
 * Its own bucket, on its own page. §6.4 says LLM-originated trades are tracked
 * separately, and §8.5 keeps this off the Dashboard on purpose — a headline win
 * rate blended with a bucket of third-size unvalidated trades answers neither
 * question well.
 *
 * The validated/unvalidated split is the number that matters: a bucket that is
 * mostly unvalidated is mostly trades no backtest ever justified. */
function OriginationPanel() {
  const validatedShare = Math.round((LLM_ORIGINATION.validated / LLM_ORIGINATION.count) * 100)

  return (
    <section
      aria-labelledby="origination-heading"
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-4"
    >
      <h2 id="origination-heading" className="text-title-lg text-on-surface">
        LLM origination
      </h2>
      <p className="mt-1 text-caption text-on-surface-variant">
        Its own P&amp;L bucket, deliberately kept off the Dashboard so the headline win rate stays a
        statement about the strategies.
      </p>

      <dl className="mt-4 grid grid-cols-2 gap-3">
        <Metric label="Originated" value={String(LLM_ORIGINATION.count)} />
        <Metric label="Win rate" value={`${LLM_ORIGINATION.winRate}%`} />
        <Metric label="Profit factor" value={String(LLM_ORIGINATION.profitFactor)} />
        <Metric
          label="Standalone P&L"
          value={formatUsd(LLM_ORIGINATION.standalonePnl, { signed: true })}
          className={signClass(LLM_ORIGINATION.standalonePnl)}
        />
      </dl>

      <div className="mt-4 border-t border-outline-variant pt-3">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-caption text-on-surface-variant">Validated / unvalidated</span>
          <span className="text-data-md text-on-surface">
            {LLM_ORIGINATION.validated} / {LLM_ORIGINATION.unvalidated}
          </span>
        </div>
        <p className="mt-1 text-caption text-on-surface-variant">
          {validatedShare}% matched a known setup and were sized normally. The rest had no match, so
          each was capped at a third of normal size — and those are the trades the Dashboard’s win
          rate excludes.
        </p>
      </div>
    </section>
  )
}

function Metric({
  label,
  value,
  className,
}: {
  label: string
  value: string
  className?: string
}) {
  return (
    <div>
      <dt className="text-caption uppercase tracking-wide text-on-surface-variant">{label}</dt>
      <dd className={`mt-0.5 text-data-lg ${className ?? 'text-on-surface'}`}>{value}</dd>
    </div>
  )
}
