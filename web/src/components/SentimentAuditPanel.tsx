import { useState } from 'react'
import { RequestFailed } from './RequestFailed'
import { TableSkeleton } from './Skeleton'
import { useSentimentAudit, useStartSentimentAudit } from '../lib/queries'
import { NEWS_SOURCE_LABEL } from '../lib/news'
import { formatDateTimeET, formatInteger, formatTenthsPct } from '../lib/format'
import {
  SENTIMENT_DEMOTE_SAMPLE,
  SENTIMENT_FLOOR,
  SENTIMENT_REPROMOTE,
  SENTIMENT_REPROMOTE_SAMPLE,
  SENTIMENT_WINDOWS,
  SENTIMENT_WINDOW_LABEL,
  accuracyTenths,
  actingCell,
  auditTriggerRefusal,
  cellCounts,
  demotedSources,
  isInsufficientSample,
  nothingAuditedYet,
  repromotionProgress,
  sourceStatusKind,
  type AuditTriggerRefusal,
  type SourceStatusKind,
} from '../lib/settings'
import type {
  AuditRunSummary,
  SentimentAccuracyBasis,
  SentimentCounts,
  SentimentSourceAudit,
  SentimentWindow,
} from '../lib/types'

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-2 align-top'

/** Status as a pill: pills read as status, rectangles as controls. None of
 * these is `bullish`/`bearish` — an accuracy audit is not a direction — and
 * a demotion is `caution`, not `error`: a signal that stopped working is not
 * a system fault. A missing status row *is* one (a damaged database), and is
 * the only `error` here. */
const STATUS_PILL: Record<SourceStatusKind, { label: string; className: string }> = {
  missing: { label: 'Status missing', className: 'bg-error-container text-on-error-container' },
  unaudited: { label: 'Unaudited', className: 'bg-neutral-container text-on-neutral-container' },
  active: { label: 'Active', className: 'bg-primary-container text-on-primary-container' },
  demoted: { label: 'Demoted', className: 'bg-caution-container text-on-caution-container' },
}

const BASIS_LABEL: Record<SentimentAccuracyBasis, string> = {
  trailing_28d: 'trailing 28 days',
  since_demotion: 'since demotion',
}

const TRIGGER_LABEL: Record<AuditRunSummary['trigger'], string> = {
  scheduled: 'scheduled',
  manual: 'started by hand',
}

const RUN_STATUS_LABEL: Record<AuditRunSummary['status'], string> = {
  running: 'still running',
  succeeded: 'succeeded',
  failed: 'failed',
}

function StatusCell({ source }: { source: SentimentSourceAudit }) {
  const kind = sourceStatusKind(source)
  const pill = STATUS_PILL[kind]
  const progress = repromotionProgress(source)
  return (
    <div>
      <span
        className={`inline-flex h-6 items-center rounded-full px-2 text-label-sm ${pill.className}`}
      >
        {pill.label}
      </span>
      <p className="mt-1 text-caption text-on-surface-variant">
        {kind === 'missing'
          ? 'No status row is stored for this source. It is shown as unaudited for display only — this is not a record that it was never audited.'
          : kind === 'unaudited'
            ? source.changedAt === null
              ? 'Never audited.'
              : `Unaudited since ${formatDateTimeET(source.changedAt)}.`
            : source.changedAt === null
              ? `${pill.label}; the change has no recorded time.`
              : `${pill.label} since ${formatDateTimeET(source.changedAt)}.`}
      </p>
      {kind !== 'missing' && source.reason ? (
        <p className="mt-1 max-w-prose text-caption text-on-surface-variant">
          Audit’s reason: {source.reason}.
        </p>
      ) : null}
      {progress.length > 0 ? (
        <ul className="mt-1 space-y-1" aria-label="Re-promotion progress">
          {progress.map((line) => (
            <li key={line} className="text-data-sm text-on-surface-variant">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

function Figure({ counts, floor }: { counts: SentimentCounts; floor: number }) {
  const tenths = accuracyTenths(counts)
  if (isInsufficientSample(counts, floor) || tenths === null) {
    // A sample-size fact, not a warning: `on-surface-variant`, never
    // `caution`. Too few labels is where every source starts.
    return (
      <span className="text-data-sm text-on-surface-variant">
        insufficient sample — {formatInteger(counts.graded)} of {formatInteger(floor)}
      </span>
    )
  }
  return (
    <span className="text-data-md text-on-surface">
      {formatTenthsPct(tenths)}{' '}
      <span className="text-data-sm text-on-surface-variant">n {formatInteger(counts.graded)}</span>
    </span>
  )
}

/** The watch/discovery split of one cell (decision 12). Display only: the
 * transitions act on the combined figure, and the label says so where the
 * split is read, not in a footnote. */
function Split({ watch, discovery }: { watch: SentimentCounts; discovery: SentimentCounts }) {
  const line = (label: string, counts: SentimentCounts) => {
    const tenths = accuracyTenths(counts)
    return (
      <div className="flex justify-end gap-2">
        <span className="text-caption text-on-surface-variant">{label}</span>
        <span className="text-data-sm text-on-surface">
          {tenths === null ? '—' : formatTenthsPct(tenths)}
        </span>
        <span className="text-data-sm text-on-surface-variant">
          n {formatInteger(counts.graded)}
        </span>
      </div>
    )
  }
  return (
    <div className="mt-2 space-y-1 border-t border-outline-variant pt-2">
      {line('Watch', watch)}
      {line('Discovery', discovery)}
      <p className="text-caption text-on-surface-variant">
        display only — transitions act on the combined figure
      </p>
    </div>
  )
}

function AccuracyCell({
  source,
  window,
  expanded,
  onToggle,
}: {
  source: SentimentSourceAudit
  window: SentimentWindow
  expanded: boolean
  onToggle: () => void
}) {
  const cell = actingCell(source, window)
  const counts = cellCounts(cell)
  const floor = source.eligibility.floor
  const label = `${NEWS_SOURCE_LABEL[source.source]}, ${SENTIMENT_WINDOW_LABEL[window]}`
  return (
    <td className={`${TD} text-right`}>
      {cell === null || counts.graded + counts.neutral === 0 ? (
        <Figure counts={counts} floor={floor} />
      ) : (
        <button
          type="button"
          aria-expanded={expanded}
          aria-label={`${label}: ${expanded ? 'hide' : 'show'} the watch and discovery split`}
          onClick={onToggle}
          className="rounded px-1 text-right hover:bg-surface-container"
        >
          <Figure counts={counts} floor={floor} />
        </button>
      )}
      <div className="text-caption text-on-surface-variant">
        {BASIS_LABEL[source.eligibility.basis]}
      </div>
      {expanded && cell !== null ? <Split watch={cell.watch} discovery={cell.discovery} /> : null}
    </td>
  )
}

function RefusalLine({ refusal }: { refusal: AuditTriggerRefusal }) {
  switch (refusal.kind) {
    case 'rate_limited':
      return (
        <p role="status" className="mt-2 max-w-prose text-caption text-on-surface-variant">
          Not started: manual runs are limited to one an hour.{' '}
          {refusal.availableAt
            ? `The next is available at ${formatDateTimeET(refusal.availableAt)}.`
            : refusal.message}
        </p>
      )
    case 'running':
      return (
        <p role="status" className="mt-2 max-w-prose text-caption text-on-surface-variant">
          Not started: a run is already in progress ({refusal.message}). Its outcome will appear
          here when it finishes.
        </p>
      )
    case 'unavailable':
      // `caution`, not `error`: a data dependency is absent or degraded and
      // nothing was started — the "pending, unvalidated" state DESIGN.md
      // gives `caution`. Nothing broke and nothing was lost.
      return (
        <p role="alert" className="mt-2 max-w-prose text-caption text-caution">
          Not started: {refusal.message}
        </p>
      )
    case 'failed':
      return (
        <p role="alert" className="mt-2 max-w-prose text-caption text-error">
          The audit did not complete: {refusal.message}
        </p>
      )
  }
}

function runLine(run: AuditRunSummary): string {
  const finished = run.finishedAt ? `, finished ${formatDateTimeET(run.finishedAt)}` : ''
  return `${formatDateTimeET(run.startedAt)} (${TRIGGER_LABEL[run.trigger]}, ${RUN_STATUS_LABEL[run.status]}${finished})`
}

/** The sentiment self-audit, as the server reports it (Phase 3 step 6).
 *
 * **Every status here is the server's.** `sentiment_source_status` is
 * written by the audit run and nothing else, so the page reads it and derives
 * only display from the counts. There is **no re-promote or reinstate
 * control** (Q6): a demoted source comes back when a run says so, and a
 * button that let a person override that would make the audit advisory. */
export function SentimentAuditPanel() {
  const query = useSentimentAudit()
  const start = useStartSentimentAudit()
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set())

  if (query.isPending) {
    return <TableSkeleton rows={2} columns={4} label="Loading the sentiment audit" />
  }
  if (query.isError) {
    return <RequestFailed error={query.error} what="the sentiment audit" />
  }

  const status = query.data
  const demoted = demotedSources(status.sources)
  const missing = status.sources.filter((s) => s.statusMissing)
  const refusal = start.isError
    ? auditTriggerRefusal(start.error, {
        requestedAt: start.submittedAt || null,
        manualAvailableAt: status.manualAvailableAt,
      })
    : null

  function toggle(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div>
      <p className="mb-4 max-w-prose text-caption text-on-surface-variant">
        A source is demoted when either window falls below{' '}
        <span className="text-data-sm text-on-surface">{SENTIMENT_FLOOR}%</span>, once each window
        has at least{' '}
        <span className="text-data-sm text-on-surface">{SENTIMENT_DEMOTE_SAMPLE}</span> graded
        labels over the trailing 28 days. A demoted source is re-promoted only when both windows
        reach <span className="text-data-sm text-on-surface">{SENTIMENT_REPROMOTE}%</span> or
        better, with at least{' '}
        <span className="text-data-sm text-on-surface">{SENTIMENT_REPROMOTE_SAMPLE}</span> graded
        labels each since the demotion. Between {SENTIMENT_FLOOR}% and {SENTIMENT_REPROMOTE}% a
        source stays where it is: an active one is not demoted and a demoted one is not
        re-promoted. Neutral labels count toward neither figure. Only the audit changes a status —
        there is no manual reinstatement.
      </p>

      {demoted.length > 0 ? (
        <p
          role="status"
          className="mb-4 rounded border border-caution bg-caution-container p-3 text-caption text-on-caution-container"
        >
          {demoted.map((d) => NEWS_SOURCE_LABEL[d.source]).join(' and ')}{' '}
          {demoted.length === 1 ? 'has' : 'have'} been demoted by the audit to display-only.
          Headlines from {demoted.length === 1 ? 'it' : 'them'} still render with a sentiment label;
          the scanner no longer reads {demoted.length === 1 ? 'it' : 'them'}. A later audit run is
          the only thing that re-promotes a source.
        </p>
      ) : null}

      {missing.length > 0 ? (
        <p role="alert" className="mb-4 max-w-prose text-caption text-error">
          {missing.map((m) => NEWS_SOURCE_LABEL[m.source]).join(' and ')}{' '}
          {missing.length === 1 ? 'has' : 'have'} no stored status row — the database is missing a
          row the engine seeds. Shown as unaudited for display only; the engine does not act on
          that fallback.
        </p>
      ) : null}

      {nothingAuditedYet(status) ? (
        <p className="mb-4 max-w-prose text-caption text-on-surface-variant">
          No audit has completed yet, so nothing below is a measurement.{' '}
          {status.nextRunAt
            ? `The first scheduled run is ${formatDateTimeET(status.nextRunAt)}.`
            : 'No scheduler is running, so no run is due.'}
        </p>
      ) : null}

      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-outline">
            <th scope="col" className={`${TH} text-left`}>
              Source
            </th>
            <th scope="col" className={`${TH} text-left`}>
              Status
            </th>
            {SENTIMENT_WINDOWS.map((w) => (
              <th key={w} scope="col" className={`${TH} text-right`}>
                {SENTIMENT_WINDOW_LABEL[w]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {status.sources.map((source) => (
            <tr key={source.source} className="border-b border-outline-variant last:border-0">
              <th scope="row" className={`${TD} text-left text-body-sm text-on-surface`}>
                {NEWS_SOURCE_LABEL[source.source]}
              </th>
              <td className={TD}>
                <StatusCell source={source} />
              </td>
              {SENTIMENT_WINDOWS.map((w) => {
                const key = `${source.source}:${w}`
                return (
                  <AccuracyCell
                    key={w}
                    source={source}
                    window={w}
                    expanded={expanded.has(key)}
                    onToggle={() => toggle(key)}
                  />
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>

      <dl className="mt-4 grid max-w-prose grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-caption">
        <dt className="text-on-surface-variant">Last run</dt>
        <dd className="text-on-surface">
          {status.lastRun ? runLine(status.lastRun) : 'No run has ever started.'}
        </dd>
        {status.accuracyRun && status.accuracyRun.id !== status.lastRun?.id ? (
          <>
            <dt className="text-on-surface-variant">Figures from</dt>
            <dd className="text-on-surface">
              the last run that succeeded, {runLine(status.accuracyRun)}
            </dd>
          </>
        ) : null}
        <dt className="text-on-surface-variant">Next run</dt>
        <dd className="text-on-surface">
          {status.nextRunAt
            ? `${formatDateTimeET(status.nextRunAt)} (scheduled)`
            : 'None scheduled — no scheduler is running.'}
        </dd>
        {status.manualAvailableAt ? (
          <>
            <dt className="text-on-surface-variant">Manual run</dt>
            <dd className="text-on-surface">
              available again at {formatDateTimeET(status.manualAvailableAt)} (one an hour)
            </dd>
          </>
        ) : null}
      </dl>

      <div className="mt-4">
        {/* Not destructive — a run reads labels and returns, and only the
            audit's own rules can move a status — so no confirm. The server
            holds the hourly limit; this button does not pre-empt it. */}
        <button
          type="button"
          onClick={() => start.mutate()}
          disabled={start.isPending}
          className="rounded border border-outline px-4 py-2 text-label-md text-on-surface transition-colors duration-base ease-standard hover:bg-surface-container disabled:pointer-events-none disabled:opacity-50"
        >
          {start.isPending ? 'Starting…' : 'Run audit now'}
        </button>
        {start.isSuccess ? (
          <p role="status" className="mt-2 max-w-prose text-caption text-on-surface-variant">
            Audit run {start.data.runId} started at {formatDateTimeET(start.data.requestedAt)}. It
            takes a few minutes; its outcome appears here when it finishes.
          </p>
        ) : null}
        {refusal ? <RefusalLine refusal={refusal} /> : null}
      </div>
    </div>
  )
}
