import { useState } from 'react'
import { Chip } from '../components/Chip'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { RequestFailed } from '../components/RequestFailed'
import { RiskLimitField } from '../components/RiskLimitField'
import { SettingsSection } from '../components/SettingsSection'
import { TableSkeleton } from '../components/Skeleton'
import { ThemeToggle } from '../components/ThemeToggle'
import { isAccountUnavailable, isApiError } from '../lib/api'
import {
  useAccount,
  useApiKeys,
  useAuditLog,
  useDataFeeds,
  useDataSources,
  useNotificationRoutes,
  useRiskLimits,
  useUpdateDataFeeds,
  useUpdateNotificationRoutes,
  useUpdateRiskLimits,
} from '../lib/queries'
import { useUIStore } from '../lib/store'
import { SENTIMENT_ACCURACY } from '../lib/mockData'
import {
  ACCOUNT_LABEL,
  DATA_PLANS,
  NOTIFICATION_EVENT_LABEL,
  type AuditLogEntry,
  type DataFeed,
  type DataSourceStatus,
  type NotificationEvent,
  type NotificationRoute,
  type RiskLimit,
} from '../lib/types'
import {
  SENTIMENT_FLOOR,
  auditFieldLabel,
  demotedSources,
  feedOptionsFor,
  feedWarning,
  isCriticalEvent,
  type NotificationChannel,
} from '../lib/settings'
import { formatDateTimeET, formatInteger, formatUsd } from '../lib/format'

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'

const SELECT =
  'rounded border border-outline bg-surface px-2 py-1 text-caption text-on-surface focus:border-primary'

/** One request covers a config log nobody edits in bulk. Above this the panel
 * says it is showing the most recent N of `total` rather than implying the
 * log is short. */
const AUDIT_PAGE_SIZE = 100

/** What stops arriving if a critical event is silenced on every channel.
 *
 * Copy rather than logic, so it lives here rather than in `settings.ts`. It has
 * to be specific: "you will not be notified" is a restatement of the checkbox,
 * while "you will not be told the engine stopped trading" is a consequence. */
const SILENCE_CONSEQUENCE: Partial<Record<NotificationEvent, string>> = {
  order_rejected:
    'You will not be told when a rule refuses an order. The first sign will be the bot doing nothing on a day you expected it to trade.',
  daily_loss_halt:
    'You will not be told when the session loss limit halts new entries. The engine will stop trading and say nothing about it.',
  engine_error:
    'You will not be told when the engine loses its Alpaca connection and halts itself. Recovery needs an explicit human resume, so it will stay halted until you happen to look.',
}

/** Status chips for a provider.
 *
 * `degraded` is `caution` and `disconnected` is `error` — a rate-limited feed
 * that is retrying is not the same event as one that is gone, and flattening
 * both into red is how you learn to ignore red. */
const SOURCE_CLASS: Record<DataSourceStatus['status'], string> = {
  connected: 'bg-bullish-container text-on-bullish-container',
  degraded: 'bg-caution-container text-on-caution-container',
  disconnected: 'bg-error-container text-on-error-container',
}

const SOURCE_LABEL: Record<DataSourceStatus['status'], string> = {
  connected: 'Connected',
  degraded: 'Degraded',
  disconnected: 'Disconnected',
}

/** A write the server refused, in its own words.
 *
 * `error`, not `caution`: a refused edit is a rule outcome, the same category
 * as a rejected order, and the page has to show the refusal rather than
 * silently reverting the control. Rule 8's reasoning applies to config too —
 * a setting that will not take with nothing anywhere saying why is a bug.
 *
 * The server's message is rendered verbatim because it names the rule that
 * refused: which plan is in force, which values are accepted, which range was
 * exceeded. A generic "could not save" throws all of that away. */
function WriteRefused({ error, what }: { error: unknown; what: string }) {
  if (!error) return null
  const message = isApiError(error)
    ? error.message
    : `The engine did not accept the change to ${what}, and gave no reason. Check that it is running.`
  return (
    <p role="alert" className="mt-3 max-w-prose text-caption text-error">
      The engine refused this change: {message} Nothing was stored, and the value above is still
      what it was.
    </p>
  )
}

/** PRD.md §8.7 — replaces the user menu. No authentication: the terminal binds
 * to 127.0.0.1 and there is one user.
 *
 * **Everything on this page except the sentiment table is served by
 * `/api/settings/*`.** Keys, ceilings, routes, feeds, provider health and the
 * audit log are all reads of the engine's own configuration, and every edit is
 * a `PUT` the server validates, stores and audit-logs. Rule 4 is unchanged by
 * that: this page edits what is *stored*, and `RiskManager.approve()` decides
 * what is allowed, reading the table rather than anything that arrived from a
 * browser. Where the server refuses a write, the refusal is what renders.
 *
 * No Paper/Cash switch here, per PRD.md §2: this page *displays* account
 * context (the header badge) without scoping its contents to it. The one place
 * the account matters is the dollar figure in a risk-limit confirm, which
 * follows whichever book is live — and which now comes from `GET /api/account`
 * rather than from a fixture. It used to be sized against $100,000 of mock
 * money, so the confirm quoted a consequence against a balance this account
 * has never had.
 */
export function Settings() {
  const accountMode = useUIStore((s) => s.accountMode)
  // The one settings fact with no endpoint behind it: the plan lives in
  // `ALPACA_DATA_PLAN`, which the server reads (unset → basic, the
  // restrictive direction) without publishing. It is used here to *mark* the
  // options an upgrade would unlock; it never decides what may be saved. The
  // server refuses an unentitled feed and that refusal is what renders.
  const dataPlan = useUIStore((s) => s.dataPlan)

  const accountQuery = useAccount()
  const keysQuery = useApiKeys()
  const limitsQuery = useRiskLimits()
  const routesQuery = useNotificationRoutes()
  const feedsQuery = useDataFeeds()
  const sourcesQuery = useDataSources()
  const auditQuery = useAuditLog({ pageSize: AUDIT_PAGE_SIZE })

  const updateLimits = useUpdateRiskLimits()
  const updateRoutes = useUpdateNotificationRoutes()
  const updateFeeds = useUpdateDataFeeds()

  /** A pending silence-both-channels change, held until confirmed. */
  const [silencing, setSilencing] = useState<{
    route: NotificationRoute
    channel: NotificationChannel
  } | null>(null)

  /* Real equity, or an honest absence. `null` while loading and after a
     failure, and the confirm says which — a percentage of a balance nobody
     could read is not a consequence, it is a number with a dollar sign on
     it. */
  const equity = accountQuery.data?.equity ?? null
  const equityNote = isAccountUnavailable(accountQuery.error)
    ? `the ${ACCOUNT_LABEL[accountMode]} account is not configured, so Alpaca reports no balance for it.`
    : accountQuery.isError
      ? `the ${ACCOUNT_LABEL[accountMode]} balance could not be read from the engine.`
      : `the ${ACCOUNT_LABEL[accountMode]} balance is still loading.`

  const plan = DATA_PLANS[dataPlan]
  const demoted = demotedSources(SENTIMENT_ACCURACY)

  const apiKeys = keysQuery.data ?? []
  const routes = routesQuery.data ?? []
  const feeds = feedsQuery.data ?? []
  const limits = limitsQuery.data ?? []

  // Both halves have to have loaded before this can be said: an empty key
  // list would otherwise claim the webhook is missing on every first paint.
  const discordConfigured =
    apiKeys.find((k) => k.envVar === 'DISCORD_WEBHOOK_URL')?.present ?? false
  const discordRoutes = routes.filter((r) => r.discord).length
  const warnAboutWebhook = keysQuery.isSuccess && !discordConfigured && discordRoutes > 0

  function toggleRoute(route: NotificationRoute, channel: NotificationChannel, next: boolean) {
    // Turning off the last channel of a critical event routes a rule-9 alert
    // to nowhere. Still permitted — every cell is editable — but not silently.
    const other: NotificationChannel = channel === 'bell' ? 'discord' : 'bell'
    if (!next && isCriticalEvent(route.event) && !route[other]) {
      setSilencing({ route, channel })
      return
    }
    commitRoute(route, channel, next)
  }

  function commitRoute(route: NotificationRoute, channel: NotificationChannel, next: boolean) {
    // The whole row goes out, not the cell: the route is one record on the
    // server, and sending half of it would let the other channel come back as
    // whatever the request happened to default it to. Spelled out rather than
    // built with a computed key, so both channels are visibly carried.
    updateRoutes.mutate([
      {
        event: route.event,
        bell: channel === 'bell' ? next : route.bell,
        discord: channel === 'discord' ? next : route.discord,
      },
    ])
  }

  return (
    // Narrower than the 1425px the data-dense pages use, and the *title rides
    // the same column as the cards* — this is a reading-and-editing surface,
    // not a scanning one, so it is measured for prose rather than for as many
    // columns as will fit. Centring only the cards left the heading stranded
    // against the window edge, pointing at nothing.
    <div className="mx-auto max-w-4xl px-4 py-12 lg:px-12">
      <h1 className="text-display-lg text-on-surface">Settings</h1>
      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        No sign-in: Corollary binds to 127.0.0.1 and has one user. Risk limits, data feeds and
        notification routing are stored by the engine, which validates and records every change —
        the audit log at the bottom of this page is the engine's, not this page's.
      </p>

      <div className="mt-8 flex flex-col gap-4">
        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="keys-heading"
          title="API keys"
          description="Read from the environment at startup. Values are never displayed here, written to the repo, or sent to the browser — the endpoint answers with a boolean per variable and has no key to leak."
        >
          {keysQuery.isPending ? (
            <TableSkeleton rows={4} columns={3} label="Reading which credentials are configured" />
          ) : keysQuery.isError ? (
            <RequestFailed error={keysQuery.error} what="credential presence" />
          ) : (
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-outline">
                  <th scope="col" className={`${TH} text-left`}>
                    Variable
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Used for
                  </th>
                  <th scope="col" className={`${TH} text-left`}>
                    Status
                  </th>
                </tr>
              </thead>
              <tbody>
                {apiKeys.map((key) => (
                  <tr key={key.envVar} className="h-8 border-b border-outline-variant last:border-0">
                    <td className={`${TD} text-data-md text-on-surface`}>{key.envVar}</td>
                    <td className={`${TD} text-caption text-on-surface-variant`}>{key.purpose}</td>
                    <td className={TD}>
                      {/* An absent *optional* key is a plain fact, not a
                          warning. The live keys are meant to be blank until
                          Phase 7, and colouring that amber would train the eye
                          to ignore the colour. */}
                      <span
                        className={`rounded-full px-2 py-0.5 text-label-sm ${
                          key.present
                            ? 'bg-bullish-container text-on-bullish-container'
                            : key.optional
                              ? 'bg-neutral-container text-on-neutral-container'
                              : 'bg-error-container text-on-error-container'
                        }`}
                      >
                        {key.present ? 'Set' : 'Not set'}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="risk-heading"
          title="Risk limits"
          description="The engine enforces these server-side and never trusts a value sent from this page — editing here changes what is stored, not what is allowed. Every change is audit-logged, in both directions."
        >
          {/* CLAUDE.md is explicit that a percentage ceiling is
              uninterpretable without this, so the page states it rather than
              leaving each reader to assume a definition. */}
          <div className="rounded border border-outline-warm bg-surface-container-low p-4">
            <h3 className="text-label-md uppercase tracking-wide text-on-surface-variant">
              What “risk” means here
            </h3>
            <dl className="mt-2 space-y-1 text-caption">
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-on-surface">Defined-risk structures</dt>
                <dd className="text-on-surface-variant">— maximum loss at expiry.</dd>
              </div>
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-on-surface">Long options</dt>
                <dd className="text-on-surface-variant">— premium paid.</dd>
              </div>
              <div className="flex flex-wrap gap-x-2">
                <dt className="text-on-surface">Undefined-risk structures</dt>
                <dd className="text-on-surface-variant">
                  — a stress loss at ±2σ of the underlying’s 20-day realized volatility.
                </dd>
              </div>
            </dl>
          </div>

          {limitsQuery.isPending ? (
            <div className="mt-2">
              <TableSkeleton rows={5} columns={2} label="Reading the configured risk ceilings" />
            </div>
          ) : limitsQuery.isError ? (
            <div className="mt-4">
              <RequestFailed error={limitsQuery.error} what="the risk ceilings" />
            </div>
          ) : (
            <>
              <div className="mt-2">
                {limits.map((limit) => (
                  <RiskLimitField
                    // Remounts on a value the server changed, so a field can
                    // never sit holding an edit against a ceiling that has
                    // already moved underneath it.
                    key={`${limit.key}:${limit.value}`}
                    limit={limit}
                    equity={equity}
                    equityNote={equityNote}
                    pending={updateLimits.isPending}
                    onCommit={(value) => updateLimits.mutate([{ key: limit.key, value }])}
                  />
                ))}
              </div>

              <WriteRefused error={updateLimits.error} what="a risk ceiling" />

              <p className="mt-3 max-w-prose text-caption text-on-surface-variant">
                {equity === null ? (
                  <>
                    Confirmations cannot quote a dollar figure right now: {equityNote} They state the
                    percentage change alone rather than a consequence measured against a balance
                    nobody could read.
                  </>
                ) : (
                  <>
                    Dollar figures in confirmations are measured against {ACCOUNT_LABEL[accountMode]}{' '}
                    equity of <span className="text-data-md text-on-surface">{formatUsd(equity)}</span>,
                    as the broker reports it.
                  </>
                )}
              </p>
            </>
          )}
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="notifications-heading"
          title="Notification routing"
          description="Which channels each event reaches. The bell is in the header; Discord posts to the configured webhook. Every cell is editable — the defaults are in PRD §10. The gate is applied when an event is emitted, so unchecking a box never erases a notification already received."
        >
          {routesQuery.isPending ? (
            <TableSkeleton rows={8} columns={3} label="Reading the notification routing matrix" />
          ) : routesQuery.isError ? (
            <RequestFailed error={routesQuery.error} what="notification routing" />
          ) : (
            <>
              <table className="w-full border-collapse">
                <thead>
                  <tr className="border-b border-outline">
                    <th scope="col" className={`${TH} text-left`}>
                      Event
                    </th>
                    <th scope="col" className={`${TH} text-center`}>
                      Bell
                    </th>
                    <th scope="col" className={`${TH} text-center`}>
                      Discord
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {routes.map((route) => {
                    const label = NOTIFICATION_EVENT_LABEL[route.event]
                    const critical = isCriticalEvent(route.event)
                    return (
                      <tr
                        key={route.event}
                        className="h-8 border-b border-outline-variant last:border-0"
                      >
                        <td className={`${TD} text-body-sm text-on-surface`}>
                          {label}
                          {critical ? (
                            /* Marked, not locked. It says why the confirm will
                               appear before you meet it. */
                            <span className="ml-2 rounded-full bg-error-container px-2 py-0.5 text-label-sm text-on-error-container">
                              Critical
                            </span>
                          ) : null}
                        </td>
                        {(['bell', 'discord'] as NotificationChannel[]).map((channel) => (
                          <td key={channel} className={`${TD} text-center`}>
                            <input
                              type="checkbox"
                              checked={route[channel]}
                              disabled={updateRoutes.isPending}
                              aria-label={`${label} — ${channel === 'bell' ? 'Bell' : 'Discord'}`}
                              onChange={(e) => toggleRoute(route, channel, e.target.checked)}
                              className="h-4 w-4 rounded accent-primary"
                            />
                          </td>
                        ))}
                      </tr>
                    )
                  })}
                </tbody>
              </table>

              <WriteRefused error={updateRoutes.error} what="notification routing" />

              {/* A route with no webhook behind it delivers nothing. Worth
                  saying on the page that configures the route, not only on the
                  page that lists the key. */}
              {warnAboutWebhook ? (
                <p className="mt-3 max-w-prose text-caption text-error">
                  {discordRoutes} events are routed to Discord but DISCORD_WEBHOOK_URL is not set, so
                  none of them will be delivered. Add it to the environment and restart the engine.
                </p>
              ) : null}
            </>
          )}
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="sources-heading"
          title="Data sources"
          description="Provider health, and which Alpaca feed each kind of request uses. Feed changes are audit-logged because they silently reinterpret every volume threshold in a strategy."
          aside={
            <span className="rounded-full bg-primary-container px-3 py-1 text-label-md text-on-primary-container">
              {plan.label}
            </span>
          }
        >
          {sourcesQuery.isPending ? (
            <TableSkeleton rows={5} columns={2} label="Reading provider health" />
          ) : sourcesQuery.isError ? (
            <RequestFailed error={sourcesQuery.error} what="provider health" />
          ) : (
            <ul className="space-y-2">
              {(sourcesQuery.data ?? []).map((source) => (
                <li
                  key={source.name}
                  className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-variant pb-2 last:border-0"
                >
                  <span className="text-body-md text-on-surface">{source.name}</span>
                  <span className="flex items-center gap-3">
                    <span className="max-w-prose text-caption text-on-surface-variant">
                      {source.detail}
                    </span>
                    <span
                      className={`whitespace-nowrap rounded-full px-2 py-0.5 text-label-sm ${SOURCE_CLASS[source.status]}`}
                    >
                      {SOURCE_LABEL[source.status]}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          )}

          <h3 className="mt-6 text-label-md uppercase tracking-wide text-on-surface-variant">
            Feed selection
          </h3>
          {feedsQuery.isPending ? (
            <div className="mt-2">
              <TableSkeleton rows={3} columns={2} label="Reading feed selection" />
            </div>
          ) : feedsQuery.isError ? (
            <div className="mt-2">
              <RequestFailed error={feedsQuery.error} what="feed selection" />
            </div>
          ) : (
            <div className="mt-2 space-y-4">
              {feeds.map((feed) => {
                const options = feedOptionsFor(feed.key, dataPlan)
                const warning = feedWarning(feed.key, feed.value)
                return (
                  <div key={feed.key} className="border-b border-outline-variant pb-4 last:border-0">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <label htmlFor={`feed-${feed.key}`} className="text-body-md text-on-surface">
                        {feed.label}
                        <span className="ml-2 text-data-md text-on-surface-variant">
                          {feed.envVar}
                        </span>
                      </label>
                      <select
                        id={`feed-${feed.key}`}
                        value={feed.value}
                        disabled={updateFeeds.isPending}
                        onChange={(e) =>
                          updateFeeds.mutate([{ key: feed.key, value: e.target.value }])
                        }
                        className={SELECT}
                      >
                        {options.map((option) => (
                          /* Disabled rather than hidden: asking Alpaca for a
                             feed the plan does not cover returns an auth error,
                             and knowing the option exists is the point of
                             showing what an upgrade buys. The mark is a hint —
                             the server is what actually refuses it. */
                          <option
                            key={option.value}
                            value={option.value}
                            disabled={option.requiresUpgrade}
                          >
                            {option.label}
                            {option.requiresUpgrade ? ' — requires Algo Trader Plus' : ''}
                          </option>
                        ))}
                      </select>
                    </div>
                    {/* The engine composes this: it is the static help plus
                        whatever the *running process* disagrees about, since a
                        stored feed only takes effect on restart. A page that
                        showed the stored value with no such sentence would be
                        claiming the engine is doing something it is not. */}
                    <p className="mt-1 max-w-prose text-caption text-on-surface-variant">
                      {feed.help}
                    </p>
                    {warning ? (
                      <p className="mt-1 max-w-prose text-caption text-caution">{warning}</p>
                    ) : null}
                  </div>
                )
              })}
            </div>
          )}

          <WriteRefused error={updateFeeds.error} what="a data feed" />

          <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
            This plan allows{' '}
            {plan.streamSymbols === null
              ? 'unlimited streamed symbols'
              : `${plan.streamSymbols} streamed symbols`}{' '}
            and {formatInteger(plan.reqPerMin)} requests per minute. That cap is why open positions
            stream while the Markets page polls — every option contract is its own symbol.
          </p>
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="theme-heading"
          title="Appearance"
          description="Light and dark are both first-class; dark drops shadows entirely rather than tinting them."
        >
          <div className="flex items-center gap-3">
            <span className="text-body-md text-on-surface">Theme</span>
            <ThemeToggle />
          </div>
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="sentiment-heading"
          title="Sentiment accuracy"
          description={`Every published label is scored weekly against that ticker's realized forward return. Below ${SENTIMENT_FLOOR}% is coin-flip territory, and a source that falls under it is demoted from a scanner input to display-only.`}
          aside={
            /* `caution`, not `error`: a fixture standing in for a pipeline
               that has not been built is a stage of the build, not a fault.
               The marker is on the panel rather than the page title because
               every other panel here is live, and an unmarked table of
               invented percentages reads as measured now that its neighbours
               are real. */
            <Chip
              variant="caution"
              title="These figures are sample data. Nothing has scored a sentiment label against realized return yet — the news and sentiment pipeline is PRD §9, a later phase."
            >
              Sample data — Phase 1
            </Chip>
          }
        >
          <p className="mb-4 max-w-prose text-caption text-caution">
            The only panel on this page that is not the engine’s own configuration. No label has
            been scored against a realized return, so nothing here can be demoted by the floor for
            real — the table shows what the readout will look like when PRD §9's pipeline exists.
          </p>

          {demoted.length > 0 ? (
            <p
              role="status"
              className="mb-4 rounded border border-caution bg-caution-container p-3 text-caption text-on-caution-container"
            >
              {demoted.map((d) => d.source).join(', ')} {demoted.length === 1 ? 'is' : 'are'} below{' '}
              {SENTIMENT_FLOOR}% and {demoted.length === 1 ? 'has been' : 'have been'} demoted to
              display-only. Headlines from {demoted.length === 1 ? 'it' : 'them'} still render with a
              sentiment label; the scanner no longer reads it.
            </p>
          ) : null}

          <table className="w-full border-collapse">
            <thead>
              <tr className="border-b border-outline">
                <th scope="col" className={`${TH} text-left`}>
                  Source
                </th>
                <th scope="col" className={`${TH} text-left`}>
                  Tier
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  1 hour
                </th>
                <th scope="col" className={`${TH} text-right`}>
                  1 day
                </th>
              </tr>
            </thead>
            <tbody>
              {SENTIMENT_ACCURACY.map((row) => (
                <tr key={row.source} className="h-8 border-b border-outline-variant last:border-0">
                  <td className={`${TD} text-body-sm text-on-surface`}>{row.source}</td>
                  <td className={`${TD} text-caption text-on-surface-variant`}>{row.tier}</td>
                  {/* `caution` on a failing figure, never `error` — a signal
                      that stopped working is not a system fault, and never
                      `bearish`, which is for losses. */}
                  <td
                    className={`${TD} text-right text-data-md ${
                      row.accuracy1h < SENTIMENT_FLOOR ? 'text-caution' : 'text-on-surface'
                    }`}
                  >
                    {row.accuracy1h}%
                  </td>
                  <td
                    className={`${TD} text-right text-data-md ${
                      row.accuracy1d < SENTIMENT_FLOOR ? 'text-caution' : 'text-on-surface'
                    }`}
                  >
                    {row.accuracy1d}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="audit-heading"
          title="Configuration audit log"
          description="Every risk limit, feed and routing change the engine recorded, newest first, with the value it replaced. One log rather than three, because on a bad day the question is simply whether anything changed first."
        >
          <AuditLog
            isPending={auditQuery.isPending}
            isError={auditQuery.isError}
            error={auditQuery.error}
            entries={auditQuery.data?.items ?? []}
            total={auditQuery.data?.total ?? 0}
            limits={limits}
            feeds={feeds}
          />
        </SettingsSection>
      </div>

      <ConfirmDialog
        open={silencing !== null}
        title={
          silencing
            ? `Silence every channel for “${NOTIFICATION_EVENT_LABEL[silencing.route.event]}”?`
            : ''
        }
        consequence={
          silencing ? (
            <>
              {SILENCE_CONSEQUENCE[silencing.route.event] ??
                'You will receive no notification of this event on any channel.'}{' '}
              It will still be recorded in the activity ledger.
            </>
          ) : (
            ''
          )
        }
        confirmLabel="Silence it"
        destructive
        onConfirm={() => {
          if (silencing) commitRoute(silencing.route, silencing.channel, false)
          setSilencing(null)
        }}
        onCancel={() => setSilencing(null)}
      />
    </div>
  )
}

/** PRD §8.7's one log, as the engine holds it.
 *
 * Field keys are resolved through `auditFieldLabel` against the *served*
 * limits and feeds rather than against the fixtures, so a row stays readable
 * if the server ever renames a label. A key it cannot resolve renders as the
 * key: a row you cannot fully interpret is still evidence that something
 * changed, and blanking it would hide that. */
function AuditLog({
  isPending,
  isError,
  error,
  entries,
  total,
  limits,
  feeds,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  entries: readonly AuditLogEntry[]
  total: number
  limits: RiskLimit[]
  feeds: DataFeed[]
}) {
  if (isPending) {
    return <TableSkeleton rows={4} columns={4} label="Reading the configuration audit log" />
  }
  if (isError) {
    return <RequestFailed error={error} what="the audit log" />
  }
  if (entries.length === 0) {
    return (
      <p className="max-w-prose text-body-md text-on-surface-variant">
        Nothing has been changed since this engine was set up. Every limit, feed and route is still
        at the value it was seeded with.
      </p>
    )
  }

  const truncated = total > entries.length

  return (
    <>
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-outline">
            <th scope="col" className={`${TH} text-left`}>
              When
            </th>
            <th scope="col" className={`${TH} text-left`}>
              Setting
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Was
            </th>
            <th scope="col" className={`${TH} text-right`}>
              Became
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id} className="h-8 border-b border-outline-variant last:border-0">
              <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                {formatDateTimeET(entry.time)}
              </td>
              <td className={`${TD} text-body-sm text-on-surface`}>
                {auditFieldLabel(entry, { limits, feeds })}
              </td>
              <td className={`${TD} text-right text-data-md text-on-surface-variant`}>
                {entry.previousValue}
              </td>
              <td className={`${TD} text-right text-data-md text-on-surface`}>{entry.newValue}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {truncated ? (
        <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
          Showing the most recent {entries.length} of {total} recorded changes.
        </p>
      ) : null}
    </>
  )
}
