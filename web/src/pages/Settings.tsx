import { useState } from 'react'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { RiskLimitField } from '../components/RiskLimitField'
import { SettingsSection } from '../components/SettingsSection'
import { ThemeToggle } from '../components/ThemeToggle'
import { useUIStore } from '../lib/store'
import {
  ACCOUNT_LABEL,
  ACCOUNT_SNAPSHOTS,
  DATA_PLANS,
  DATA_SOURCES,
  NOTIFICATION_EVENT_LABEL,
  SENTIMENT_ACCURACY,
  type DataSourceStatus,
  type NotificationEvent,
} from '../lib/mockData'
import { totalEquity } from '../lib/account'
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

/** PRD.md §8.7 — replaces the user menu. No authentication: the terminal binds
 * to 127.0.0.1 and there is one user.
 *
 * No Paper/Cash switch here, per PRD.md §2: this page *displays* account
 * context (the header badge) without scoping its contents to it. The one place
 * the account matters is the dollar figure in a risk-limit confirm, which
 * follows whichever book is live.
 */
export function Settings() {
  const accountMode = useUIStore((s) => s.accountMode)
  const positions = useUIStore((s) => s.openPositions[s.accountMode])
  const riskLimits = useUIStore((s) => s.riskLimits)
  const auditLog = useUIStore((s) => s.auditLog)
  const notificationRoutes = useUIStore((s) => s.notificationRoutes)
  const dataFeeds = useUIStore((s) => s.dataFeeds)
  const dataPlan = useUIStore((s) => s.dataPlan)
  const apiKeys = useUIStore((s) => s.apiKeys)
  const setRiskLimit = useUIStore((s) => s.setRiskLimit)
  const setNotificationRoute = useUIStore((s) => s.setNotificationRoute)
  const setDataFeed = useUIStore((s) => s.setDataFeed)

  /** A pending silence-both-channels change, held until confirmed. */
  const [silencing, setSilencing] = useState<{
    event: NotificationEvent
    channel: NotificationChannel
  } | null>(null)

  const equity = totalEquity(ACCOUNT_SNAPSHOTS[accountMode].cash, positions)
  const plan = DATA_PLANS[dataPlan]
  const demoted = demotedSources(SENTIMENT_ACCURACY)

  const discordConfigured = apiKeys.find((k) => k.envVar === 'DISCORD_WEBHOOK_URL')?.present ?? false
  const discordRoutes = notificationRoutes.filter((r) => r.discord).length

  function toggleRoute(event: NotificationEvent, channel: NotificationChannel, next: boolean) {
    const route = notificationRoutes.find((r) => r.event === event)
    if (!route) return

    // Turning off the last channel of a critical event routes a rule-9 alert
    // to nowhere. Still permitted — every cell is editable — but not silently.
    const other: NotificationChannel = channel === 'bell' ? 'discord' : 'bell'
    if (!next && isCriticalEvent(event) && !route[other]) {
      setSilencing({ event, channel })
      return
    }
    setNotificationRoute(event, channel, next)
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
        No sign-in: Corollary binds to 127.0.0.1 and has one user. Changes to risk limits, data
        feeds and notification routing are recorded in the audit log at the bottom of this page.
      </p>

      <div className="mt-8 flex flex-col gap-4">
        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="keys-heading"
          title="API keys"
          description="Read from the environment at startup. Values are never displayed here, written to the repo, or sent to the browser — only whether each one is set."
        >
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
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="risk-heading"
          title="Risk limits"
          description="The engine enforces these server-side and never trusts a value sent from this page — editing here changes what is stored, not what is allowed. Every change is audit-logged."
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

          <div className="mt-2">
            {riskLimits.map((limit) => (
              <RiskLimitField
                key={limit.key}
                limit={limit}
                equity={equity}
                onCommit={(value) => setRiskLimit(limit.key, value)}
              />
            ))}
          </div>

          <p className="mt-3 text-caption text-on-surface-variant">
            Dollar figures in confirmations are measured against {ACCOUNT_LABEL[accountMode]} equity
            of {formatUsd(equity)}.
          </p>
        </SettingsSection>

        {/* ---------------------------------------------------------------- */}
        <SettingsSection
          id="notifications-heading"
          title="Notification routing"
          description="Which channels each event reaches. The bell is in the header; Discord posts to the configured webhook. Every cell is editable — the defaults are in PRD §10."
        >
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
              {notificationRoutes.map((route) => {
                const label = NOTIFICATION_EVENT_LABEL[route.event]
                const critical = isCriticalEvent(route.event)
                return (
                  <tr key={route.event} className="h-8 border-b border-outline-variant last:border-0">
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
                          aria-label={`${label} — ${channel === 'bell' ? 'Bell' : 'Discord'}`}
                          onChange={(e) => toggleRoute(route.event, channel, e.target.checked)}
                          className="h-4 w-4 rounded accent-primary"
                        />
                      </td>
                    ))}
                  </tr>
                )
              })}
            </tbody>
          </table>

          {/* A route with no webhook behind it delivers nothing. Worth saying
              on the page that configures the route, not only on the page that
              lists the key. */}
          {!discordConfigured && discordRoutes > 0 ? (
            <p className="mt-3 max-w-prose text-caption text-error">
              {discordRoutes} events are routed to Discord but DISCORD_WEBHOOK_URL is not set, so
              none of them will be delivered. Add it to the environment and restart the engine.
            </p>
          ) : null}
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
          <ul className="space-y-2">
            {DATA_SOURCES.map((source) => (
              <li
                key={source.name}
                className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-variant pb-2 last:border-0"
              >
                <span className="text-body-md text-on-surface">{source.name}</span>
                <span className="flex items-center gap-3">
                  <span className="text-caption text-on-surface-variant">{source.detail}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-label-sm ${SOURCE_CLASS[source.status]}`}
                  >
                    {SOURCE_LABEL[source.status]}
                  </span>
                </span>
              </li>
            ))}
          </ul>

          <h3 className="mt-6 text-label-md uppercase tracking-wide text-on-surface-variant">
            Feed selection
          </h3>
          <div className="mt-2 space-y-4">
            {dataFeeds.map((feed) => {
              const options = feedOptionsFor(feed.key, dataPlan)
              const warning = feedWarning(feed.key, feed.value)
              return (
                <div key={feed.key} className="border-b border-outline-variant pb-4 last:border-0">
                  <div className="flex flex-wrap items-center justify-between gap-3">
                    <label htmlFor={`feed-${feed.key}`} className="text-body-md text-on-surface">
                      {feed.label}
                      <span className="ml-2 text-data-md text-on-surface-variant">{feed.envVar}</span>
                    </label>
                    <select
                      id={`feed-${feed.key}`}
                      value={feed.value}
                      onChange={(e) => setDataFeed(feed.key, e.target.value)}
                      className={SELECT}
                    >
                      {options.map((option) => (
                        /* Disabled rather than hidden: asking Alpaca for a
                           feed the plan does not cover returns an auth error,
                           and knowing the option exists is the point of
                           showing what an upgrade buys. */
                        <option key={option.value} value={option.value} disabled={option.requiresUpgrade}>
                          {option.label}
                          {option.requiresUpgrade ? ' — requires Algo Trader Plus' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                  <p className="mt-1 max-w-prose text-caption text-on-surface-variant">{feed.help}</p>
                  {warning ? (
                    <p className="mt-1 max-w-prose text-caption text-caution">{warning}</p>
                  ) : null}
                </div>
              )
            })}
          </div>

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
        >
          {demoted.length > 0 ? (
            <p role="status" className="mb-4 rounded border border-caution bg-caution-container p-3 text-caption text-on-caution-container">
              {demoted.map((d) => d.source).join(', ')} {demoted.length === 1 ? 'is' : 'are'} below{' '}
              {SENTIMENT_FLOOR}% and{' '}
              {demoted.length === 1 ? 'has been' : 'have been'} demoted to display-only. Headlines
              from {demoted.length === 1 ? 'it' : 'them'} still render with a sentiment label; the
              scanner no longer reads it.
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
          description="Every risk limit, feed and routing change, newest first, with the value it replaced. One log rather than three, because on a bad day the question is simply whether anything changed first."
        >
          {auditLog.length === 0 ? (
            <p className="max-w-prose text-body-md text-on-surface-variant">
              Nothing has been changed since this account was set up. Every limit, feed and route is
              still at its default.
            </p>
          ) : (
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
                {auditLog.map((entry) => (
                  <tr key={entry.id} className="h-8 border-b border-outline-variant last:border-0">
                    <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                      {formatDateTimeET(entry.time)}
                    </td>
                    <td className={`${TD} text-body-sm text-on-surface`}>
                      {auditFieldLabel(entry)}
                    </td>
                    <td className={`${TD} text-right text-data-md text-on-surface-variant`}>
                      {entry.previousValue}
                    </td>
                    <td className={`${TD} text-right text-data-md text-on-surface`}>
                      {entry.newValue}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </SettingsSection>
      </div>

      <ConfirmDialog
        open={silencing !== null}
        title={
          silencing
            ? `Silence every channel for “${NOTIFICATION_EVENT_LABEL[silencing.event]}”?`
            : ''
        }
        consequence={
          silencing ? (
            <>
              {SILENCE_CONSEQUENCE[silencing.event] ??
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
          if (silencing) setNotificationRoute(silencing.event, silencing.channel, false)
          setSilencing(null)
        }}
        onCancel={() => setSilencing(null)}
      />
    </div>
  )
}
