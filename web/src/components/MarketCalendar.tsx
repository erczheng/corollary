import { useState, type FormEvent, type ReactNode } from 'react'
import { ConfirmDialog } from './ConfirmDialog'
import { RequestFailed } from './RequestFailed'
import { SectionLabel } from './SectionLabel'
import { CALENDAR_EVENT_NOT_EDITABLE, isApiError } from '../lib/api'
import {
  IPO_STATUS_LABEL,
  JOB_LABEL,
  JOB_STATE_LABEL,
  SOURCE_PHRASE,
  calendarWindow,
  economicFigures,
  emptyReading,
  etClock,
  etInstant,
  groupByType,
  jobTone,
  removeConsequence,
  timeSlot,
  uncoveredAfter,
  type JobTone,
} from '../lib/calendar'
import {
  formatDateOnly,
  formatDecimalFigure,
  formatInteger,
  formatPriceRange,
  formatSessionDay,
  formatTimeET,
  marketToday,
} from '../lib/format'
import {
  useAddCalendarEntry,
  useCalendar,
  useEditCalendarEntry,
  useRemoveCalendarEntry,
} from '../lib/queries'
import {
  CALENDAR_TYPE_LABEL,
  type CalendarEvent,
  type CalendarJobNotice,
  type CalendarRange,
  type CalendarSeedGap,
  type CalendarSource,
} from '../lib/types'

/**
 * The News page's market calendar, from `GET /api/calendar` (Phase 3 step 7).
 *
 * Real data, so no `FixtureMarker`. Three things it must never do:
 *
 * - **regroup by `at`** — the served `date` is the Eastern session and the
 *   only grouping key (an event at 00:30Z belongs to the evening before);
 * - **print a time it was not given** — earnings read a session, date-only
 *   central-bank rows read the bank's own local date, the rest "All day";
 * - **say "nothing scheduled" about dates nobody asked about** — every
 *   job's notice is said aloud, and an empty range reads as failing,
 *   incomplete or quiet (`emptyReading`), which are different statements.
 *
 * The manual-entry form is the one write control on the page (decision 9).
 */

const INPUT =
  'rounded border border-outline bg-surface px-2 py-1 text-caption text-on-surface placeholder:text-on-surface-variant focus:border-primary'
const TEXT_BUTTON =
  'rounded px-2 py-0.5 text-label-sm text-on-surface-variant hover:bg-surface-container hover:text-on-surface disabled:opacity-50'
const PRIMARY_BUTTON =
  'rounded bg-primary px-3 py-1 text-label-sm text-on-primary disabled:opacity-50'

/** Figures are monospaced with tabular digits, like every number here. */
function Num({ children }: { children: string }) {
  return <span className="font-mono tabular-nums">{children}</span>
}

function rangePhrase(range: Pick<CalendarRange, 'start' | 'end'>): string {
  return `${formatSessionDay(range.start)} – ${formatSessionDay(range.end)}`
}

export function MarketCalendar({ today }: { today?: string }) {
  const dates = calendarWindow(today ?? marketToday())
  const calendar = useCalendar(dates)
  const remove = useRemoveCalendarEntry()
  const [editing, setEditing] = useState<string | null>(null)
  const [removing, setRemoving] = useState<CalendarEvent | null>(null)
  /** The source of the row last sent for removal, so a 409 can name it. */
  const [removedSource, setRemovedSource] = useState<CalendarSource | null>(null)
  const data = calendar.data

  function confirmRemove() {
    if (!removing) return
    const target = removing
    setRemoving(null)
    setRemovedSource(target.source)
    remove.mutate(target.id)
  }

  return (
    <section aria-labelledby="market-calendar-heading" className="mt-8">
      <SectionLabel
        id="market-calendar-heading"
        aside={
          <span className="text-caption text-on-surface-variant">
            {formatDateOnly(dates.from)} – {formatDateOnly(dates.to)} · Eastern dates and
            times
          </span>
        }
      >
        Market calendar
      </SectionLabel>

      {calendar.isError && data ? (
        // A failed refresh over a calendar already on screen: keep it, and
        // say how old it is. Blanking it would read as an empty week.
        <div className="mt-2" role="status">
          <RequestFailed error={calendar.error} what="the market calendar" />
          <p className="mt-1 text-caption text-on-surface-variant">
            Showing the calendar as last read at {formatTimeET(new Date(calendar.dataUpdatedAt).toISOString())}{' '}
            ET — it may be stale.
          </p>
        </div>
      ) : null}

      {calendar.isPending ? (
        <CalendarSkeleton />
      ) : calendar.isError && !data ? (
        <div className="py-6">
          <RequestFailed error={calendar.error} what="the market calendar" />
        </div>
      ) : data ? (
        <>
          {data.days.length === 0 ? (
            <EmptyRange range={data} />
          ) : (
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {data.days.map((day) => (
                <div
                  key={day.date}
                  role="group"
                  aria-label={formatSessionDay(day.date)}
                  className="rounded-lg border border-outline-warm bg-surface-container-lowest p-3"
                >
                  {/* The served date, formatted in UTC: a bare YYYY-MM-DD is
                      UTC midnight, and rendered in ET it names the day before. */}
                  <p className="text-caption uppercase tracking-wide text-on-surface">
                    {formatSessionDay(day.date)}
                  </p>
                  <div className="mt-2 space-y-2">
                    {groupByType(day.events).map(([type, group]) => (
                      <div key={type}>
                        <p className="text-caption uppercase tracking-wide text-on-surface-variant">
                          {CALENDAR_TYPE_LABEL[type]}
                        </p>
                        <ul className="space-y-1">
                          {group.map((e) =>
                            editing === e.id ? (
                              <li key={e.id}>
                                <ManualEntryForm
                                  event={e}
                                  today={dates.from}
                                  onDone={() => setEditing(null)}
                                />
                              </li>
                            ) : (
                              <EventRow
                                key={e.id}
                                event={e}
                                range={data}
                                onEdit={() => setEditing(e.id)}
                                onRemove={() => setRemoving(e)}
                              />
                            ),
                          )}
                        </ul>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
          {remove.isError ? (
            <div className="mt-2">
              <WriteRefusal error={remove.error} source={removedSource} action="removed" />
            </div>
          ) : null}
          <Notices range={data} />
        </>
      ) : null}

      <div className="mt-4 rounded-lg border border-outline-warm bg-surface-container-lowest p-3">
        <h3 className="text-caption uppercase tracking-wide text-on-surface-variant">
          Add a geopolitical entry
        </h3>
        <p className="mt-1 text-caption text-on-surface-variant">
          Summits, elections, deadlines — anything no feed carries. Kept on this calendar only; it
          governs nothing in the engine.
        </p>
        <ManualEntryForm today={dates.from} />
      </div>

      <ConfirmDialog
        open={removing !== null}
        title="Remove calendar entry"
        consequence={removing ? removeConsequence(removing) : ''}
        confirmLabel="Remove entry"
        destructive
        onConfirm={confirmRemove}
        onCancel={() => setRemoving(null)}
      />
    </section>
  )
}

/* ---------------------------------------------------------------------- *
 * One row
 * ---------------------------------------------------------------------- */

function EventRow({
  event: e,
  range,
  onEdit,
  onRemove,
}: {
  event: CalendarEvent
  range: CalendarRange
  onEdit: () => void
  onRemove: () => void
}) {
  return (
    <li className="text-caption text-on-surface">
      <p>
        <TimeSlotView event={e} />{' '}
        {e.ticker ? <span className="font-semibold">{e.ticker}</span> : null} {e.title}
      </p>
      <EventDetail event={e} range={range} />
      {e.editable ? (
        <span className="mt-0.5 flex gap-1">
          <button
            type="button"
            onClick={onEdit}
            aria-label={`Edit ${e.title}`}
            className={TEXT_BUTTON}
          >
            Edit
          </button>
          <button
            type="button"
            onClick={onRemove}
            aria-label={`Remove ${e.title}`}
            className={TEXT_BUTTON}
          >
            Remove
          </button>
        </span>
      ) : null}
    </li>
  )
}

function TimeSlotView({ event }: { event: CalendarEvent }) {
  const slot = timeSlot(event)
  switch (slot.kind) {
    case 'time':
      return <span className="text-on-surface-variant">{formatTimeET(slot.at)}</span>
    case 'local-date':
      // The tooltip is pointer-only on a span, so the reason rides along for
      // a screen reader in the same words — the FixtureMarker precedent.
      return (
        <span className="text-on-surface-variant">
          <span title={slot.detail} className="underline decoration-dotted underline-offset-2">
            {slot.text}
          </span>
          <span className="sr-only"> ({slot.detail})</span>
        </span>
      )
    default:
      return <span className="text-on-surface-variant">{slot.text}</span>
  }
}

function EventDetail({ event: e, range }: { event: CalendarEvent; range: CalendarRange }) {
  if (e.type === 'earnings') {
    if (e.estimate === null && e.actual === null) return null
    return (
      <p className="text-on-surface-variant">
        {e.estimate !== null ? (
          <>
            EPS est. <Num>{formatDecimalFigure(e.estimate, e.unit)}</Num>
          </>
        ) : null}
        {e.estimate !== null && e.actual !== null ? ' · ' : null}
        {e.actual !== null ? (
          <>
            Actual <Num>{formatDecimalFigure(e.actual, e.unit)}</Num>
          </>
        ) : null}
      </p>
    )
  }

  if (e.type === 'economic') {
    const figures = economicFigures(e, range)
    return (
      <p className="text-on-surface-variant">
        {figures.kind === 'pending' ? (
          <span title={range.notices.releaseFigures.reason}>
            Prior and actual pending an owner decision
          </span>
        ) : (
          <>
            Prior{' '}
            {figures.prior !== null ? (
              <Num>{formatDecimalFigure(figures.prior, figures.unit)}</Num>
            ) : (
              'not available'
            )}
            {' · '}Actual{' '}
            {figures.actual !== null ? (
              <Num>{formatDecimalFigure(figures.actual, figures.unit)}</Num>
            ) : (
              'after release'
            )}
          </>
        )}
        {e.consensus === 'unavailable' ? ' · Consensus not available' : null}
      </p>
    )
  }

  if (e.type === 'ipo') {
    const price = formatPriceRange(e.priceLow, e.priceHigh)
    const parts: ReactNode[] = [
      e.exchange ?? 'Exchange not given',
      price !== null ? <Num key="price">{price}</Num> : 'Price range not set',
      e.shares !== null ? (
        <span key="shares">
          <Num>{formatInteger(e.shares)}</Num> shares
        </span>
      ) : (
        'Share count not given'
      ),
      e.ipoStatus !== null ? IPO_STATUS_LABEL[e.ipoStatus] : 'Status not given',
    ]
    return (
      <p className="text-on-surface-variant">
        {parts.map((part, i) => (
          <span key={i}>
            {i > 0 ? <span aria-hidden="true"> · </span> : null}
            <span>{part}</span>
          </span>
        ))}
      </p>
    )
  }

  return null
}

/* ---------------------------------------------------------------------- *
 * States
 * ---------------------------------------------------------------------- */

function CalendarSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
    >
      <span className="sr-only">Loading the market calendar</span>
      {[0, 1, 2, 3].map((i) => (
        <div
          key={i}
          aria-hidden="true"
          className="space-y-2 rounded-lg border border-outline-warm bg-surface-container-lowest p-3"
        >
          <span className="block h-3 w-20 animate-pulse rounded bg-surface-container-high" />
          <span className="block h-3 w-full animate-pulse rounded bg-surface-container-high" />
          <span className="block h-3 w-2/3 animate-pulse rounded bg-surface-container-high" />
        </div>
      ))}
    </div>
  )
}

/** An empty range, read three ways — see `emptyReading`. */
function EmptyRange({ range }: { range: CalendarRange }) {
  const reading = emptyReading(range)
  const span = rangePhrase(range)
  if (reading === 'failing') {
    return (
      <p className="py-6 text-body-md text-error">
        No events to show for {span}, and that is not a quiet calendar: a calendar feed is failing
        or was refused, so this says nothing about what is scheduled. The coverage notes below say
        which.
      </p>
    )
  }
  if (reading === 'incomplete') {
    return (
      <p className="py-6 text-body-md text-on-surface-variant">
        No events stored for {span} yet. Some calendars have not been fetched in this process or do
        not reach the whole range, so an empty panel here is not a quiet week — the coverage notes
        below say which.
      </p>
    )
  }
  return (
    <p className="py-6 text-body-md text-on-surface-variant">
      Nothing scheduled {span}. Every calendar feed has run and covers the whole range.
    </p>
  )
}

const TONE_PILL: Record<JobTone, string> = {
  error: 'bg-error-container text-on-error-container',
  caution: 'bg-caution-container text-on-caution-container',
  calm: 'bg-neutral-container text-on-neutral-container',
}

const TONE_TEXT: Record<JobTone, string> = {
  error: 'text-error',
  caution: 'text-on-surface',
  calm: 'text-on-surface-variant',
}

/** Everything the panel has to say aloud: each job's state and sentence,
 * where its coverage stops, each central-bank seed gap, and why economic
 * figures are blank. */
function Notices({ range }: { range: CalendarRange }) {
  const { jobs, seedGaps, releaseFigures } = range.notices
  return (
    <div className="mt-4 space-y-3">
      <div>
        <h3 className="text-caption uppercase tracking-wide text-on-surface-variant">Coverage</h3>
        {jobs.length === 0 ? (
          <p className="mt-1 text-caption text-on-surface-variant">
            The engine reported no calendar feeds, so nothing here can be called complete.
          </p>
        ) : (
          <ul aria-label="Calendar coverage" className="mt-1 space-y-1">
            {jobs.map((job) => (
              <JobLine key={job.job} job={job} end={range.end} />
            ))}
          </ul>
        )}
      </div>

      {seedGaps.length > 0 ? (
        <div>
          <h3 className="text-caption uppercase tracking-wide text-on-surface-variant">
            Central-bank dates
          </h3>
          <ul aria-label="Central-bank seed gaps" className="mt-1 space-y-1">
            {seedGaps.map((gap) => (
              <li
                key={`${gap.bank ?? 'all'}-${gap.year}-${gap.kind}`}
                className="text-caption text-on-surface"
              >
                {seedGapLead(gap)}{' '}
                <span className="text-on-surface-variant">{gap.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {releaseFigures.state === 'pending_owner_decision' ? (
        <p className="text-caption text-on-surface-variant">
          <span className="text-on-surface">Economic prior and actual are pending.</span>{' '}
          {releaseFigures.reason} Consensus is not available on any release — no free source sells
          it.
        </p>
      ) : null}
    </div>
  )
}

function JobLine({ job, end }: { job: CalendarJobNotice; end: string }) {
  const tone = jobTone(job.state)
  const uncovered = uncoveredAfter(job, end)
  return (
    <li className={`text-caption ${TONE_TEXT[tone]}`}>
      <span
        className={`mr-2 inline-flex h-5 items-center rounded-full px-2 text-label-sm ${TONE_PILL[tone]}`}
      >
        {JOB_LABEL[job.job]}: {JOB_STATE_LABEL[job.state]}
      </span>
      {job.message}
      {uncovered !== null && uncovered !== 'all' ? (
        <span className="text-on-surface">
          {' '}
          Dates after {formatSessionDay(uncovered)} are not covered for{' '}
          {JOB_LABEL[job.job].toLowerCase()} — an empty day there means not fetched, not nothing
          scheduled.
        </span>
      ) : null}
    </li>
  )
}

function seedGapLead(gap: CalendarSeedGap): string {
  const who = gap.bank ?? 'The central-bank seed'
  switch (gap.kind) {
    case 'no_file':
      return `${who} ${gap.year}: no seed file, so no decisions for this year are on the calendar.`
    case 'unpublished':
      return `${gap.bank ?? 'A bank'} has not published ${gap.year} dates.`
    case 'partial':
      return `${who} ${gap.year} seeded only from ${gap.coversFrom ? formatDateOnly(gap.coversFrom) : 'part of the year'}.`
    case 'unreadable':
      return `The ${gap.year} central-bank seed failed validation, so no ${gap.year} decisions are on the calendar.`
  }
}

/* ---------------------------------------------------------------------- *
 * The manual-entry form — the one write control on the page
 * ---------------------------------------------------------------------- */

/** A write the server refused, in its words. A 409 is a vendor or seed
 * row; anything else (a 422 above all) is the server's own sentence. In
 * `error`, never `bearish`: a refusal is a rule outcome, not a loss. */
function WriteRefusal({
  error,
  source,
  action,
}: {
  error: unknown
  source: CalendarSource | null
  action: 'saved' | 'removed'
}) {
  if (isApiError(error) && error.code === CALENDAR_EVENT_NOT_EDITABLE) {
    return (
      <p role="alert" className="text-caption text-error">
        {source
          ? `This row comes from ${SOURCE_PHRASE[source]} and can't be edited or removed — only manual geopolitical entries can.`
          : `${error.message}.`}
      </p>
    )
  }
  if (isApiError(error) && error.status === 422) {
    return (
      <p role="alert" className="text-caption text-error">
        Not {action}: {error.message}
      </p>
    )
  }
  return <RequestFailed error={error} what="the calendar entry" />
}

function ManualEntryForm({
  event,
  today,
  onDone,
}: {
  /** Present when editing; absent when adding. */
  event?: CalendarEvent
  today: string
  onDone?: () => void
}) {
  const add = useAddCalendarEntry()
  const edit = useEditCalendarEntry()
  const mutation = event ? edit : add
  const [title, setTitle] = useState(event?.title ?? '')
  const [date, setDate] = useState(event?.date ?? today)
  const [time, setTime] = useState(event?.at ? etClock(event.at) : '')
  const idPrefix = event ? `calendar-edit-${event.id}` : 'calendar-add'

  function reset() {
    if (mutation.isError) mutation.reset()
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    const entry = {
      title: title.trim(),
      date,
      at: time === '' ? null : etInstant(date, time),
    }
    if (event) {
      edit.mutate({ id: event.id, entry }, { onSuccess: () => onDone?.() })
    } else {
      add.mutate(entry, {
        onSuccess: () => {
          setTitle('')
          setTime('')
        },
      })
    }
  }

  return (
    <form
      onSubmit={submit}
      aria-label={event ? `Edit ${event.title}` : 'Add a calendar entry'}
      className="mt-2 space-y-2"
    >
      <div className="flex flex-wrap items-end gap-2">
        <label htmlFor={`${idPrefix}-title`} className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-label-sm text-on-surface-variant">Title</span>
          <input
            id={`${idPrefix}-title`}
            value={title}
            onChange={(e) => {
              setTitle(e.target.value)
              reset()
            }}
            placeholder="G20 summit"
            maxLength={200}
            className={`${INPUT} min-w-[10rem]`}
          />
        </label>
        <label htmlFor={`${idPrefix}-date`} className="flex flex-col gap-0.5">
          <span className="text-label-sm text-on-surface-variant">Date</span>
          <input
            id={`${idPrefix}-date`}
            type="date"
            value={date}
            onChange={(e) => {
              setDate(e.target.value)
              reset()
            }}
            className={`${INPUT} font-mono tabular-nums`}
          />
        </label>
        <label htmlFor={`${idPrefix}-time`} className="flex flex-col gap-0.5">
          <span className="text-label-sm text-on-surface-variant">Time, ET (optional)</span>
          <input
            id={`${idPrefix}-time`}
            type="time"
            value={time}
            onChange={(e) => {
              setTime(e.target.value)
              reset()
            }}
            className={`${INPUT} font-mono tabular-nums`}
          />
        </label>
        <button
          type="submit"
          disabled={mutation.isPending || title.trim() === '' || date === ''}
          className={PRIMARY_BUTTON}
        >
          {event ? 'Save' : 'Add entry'}
        </button>
        {event ? (
          <button type="button" onClick={onDone} className={TEXT_BUTTON}>
            Cancel
          </button>
        ) : null}
      </div>
      {mutation.isError ? (
        <WriteRefusal error={mutation.error} source={event?.source ?? null} action="saved" />
      ) : null}
    </form>
  )
}
