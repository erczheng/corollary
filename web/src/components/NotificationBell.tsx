import { useEffect, useRef, useState } from 'react'
import { BellIcon, XIcon } from './icons'
import { RequestFailed } from './RequestFailed'
import { useAccountScope, useDismissNotification, useMarkNotificationsRead, useNotifications } from '../lib/queries'
import {
  SEVERITY_CLASS,
  SEVERITY_DOT_CLASS,
  unreadCount,
  visibleNotifications,
} from '../lib/notifications'
import { NOTIFICATION_EVENT_LABEL } from '../lib/types'
import { formatDateTimeET, formatTimeET } from '../lib/format'

const ICON_BUTTON =
  'flex h-9 w-9 items-center justify-center rounded transition-colors duration-base ease-standard'

const ICON_IDLE = 'text-on-surface-variant hover:bg-surface-container-low hover:text-on-surface'

/** The in-app bell of PRD.md §10 — the channel that receives every event the
 * routing matrix sends it.
 *
 * **Reads `GET /api/notifications` and nothing else.** The engine writes the
 * row when it emits, with the routing gate applied there; this component
 * applies no routing filter of its own, so unchecking a route in Settings
 * cannot retroactively erase a notification already received. Nothing the
 * Phase 1 fixture broker (`store.tick()`) generates reaches this panel.
 *
 * A panel anchored to the bell rather than a page. A notification you have to
 * navigate to is one you miss while watching a position, and PRD.md §11 does
 * not list a notifications page among the app's screens.
 *
 * **Opening does not mark anything read.** Glancing at a badge and closing the
 * panel is not the same act as reading eight notifications, and a panel that
 * silently clears itself on open destroys the one piece of state that tells
 * you what you have not yet seen. Clearing is an explicit button.
 *
 * **An unreachable engine is not an empty bell.** A failed read with nothing
 * cached says so in `error`; a failed poll *after* a good read keeps the list
 * and says it may be out of date. "Nothing yet" is only ever said about a
 * list the server actually returned empty. The same holds on the *closed*
 * bell: either failure puts an `error` marker on the glyph, and while the
 * first read is pending the tooltip says "loading", not "no unread".
 */
export function NotificationBell() {
  const accountMode = useAccountScope()
  const query = useNotifications(accountMode)
  const markRead = useMarkNotificationsRead(accountMode)
  const dismiss = useDismissNotification(accountMode)

  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Scoped to the book on screen. The server already scopes the feed; this is
  // the same rule applied again where it is written down (notifications.ts),
  // so the badge and the list can never count different sets. Engine-level
  // events carry no account and appear in both.
  const all = query.data ?? []
  const visible = visibleNotifications(all, accountMode)
  const unread = unreadCount(all, accountMode)
  const unreadIds = visible.filter((n) => !n.read).map((n) => n.id)

  // Failed with nothing to show, versus failed with a good list still cached.
  // TanStack keeps `data` on a failed refetch, so these are two conditions.
  const failedEmpty = query.isError && query.data === undefined
  const failedStale = query.isError && query.data !== undefined
  const failed = failedEmpty || failedStale
  const actionError = markRead.error ?? dismiss.error

  // The mutations are not keyed by account and the bell does not remount on a
  // switch, so without this a dismiss refused in Paper would keep its failure
  // alert up in the Cash panel through every successful poll that followed.
  // `reset()` also detaches an in-flight write, so a Paper failure landing
  // after the switch cannot surface under Cash either. Guarded on the previous
  // mode so it fires on a change, never on an ordinary re-render.
  const lastMode = useRef(accountMode)
  const resetMarkRead = markRead.reset
  const resetDismiss = dismiss.reset
  useEffect(() => {
    if (lastMode.current === accountMode) return
    lastMode.current = accountMode
    resetMarkRead()
    resetDismiss()
  }, [accountMode, resetMarkRead, resetDismiss])

  useEffect(() => {
    if (!open) return

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    function onPointerDown(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }

    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('mousedown', onPointerDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  // The count goes in the accessible name, not only in the badge — a screen
  // reader gets "3 unread" rather than a bare "Notifications" beside a red dot
  // it cannot see. An unreadable feed says so rather than claiming no unread,
  // and a feed not yet read makes no claim about its contents at all.
  const staleCount = unread > 0 ? `${unread} unread as of the last good read, ` : ''
  const label = query.isPending
    ? 'Notifications — loading'
    : failedEmpty
      ? 'Notifications — unavailable'
      : failedStale
        ? `Notifications — ${staleCount}may be out of date`
        : unread > 0
          ? `Notifications — ${unread} unread`
          : 'Notifications'
  const title = query.isPending
    ? 'Notifications — loading'
    : failedEmpty
      ? 'Notifications unavailable — the engine did not answer'
      : failedStale
        ? `The last refresh failed — ${staleCount}may be out of date`
        : unread > 0
          ? `${unread} unread`
          : 'Notifications — no unread'

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={title}
        className={`${ICON_BUTTON} ${
          open
            ? 'bg-surface-container-low text-on-surface'
            : failed
              ? 'text-error hover:bg-surface-container-low'
              : ICON_IDLE
        }`}
      >
        {/* h-4 w-4 explicit: a custom className on these icon components
            replaces their default sizing rather than merging with it. */}
        <BellIcon className="h-4 w-4" />
        {unread > 0 ? (
          <span
            aria-hidden="true"
            className="absolute right-1 top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-error px-1 text-[10px] font-semibold leading-none text-on-error"
          >
            {unread > 9 ? '9+' : unread}
          </span>
        ) : null}
        {/* The closed bell must not look healthy when the feed is unreadable:
            the header has no other engine-status indicator, and a quiet bell
            reads as "nothing to report" at exactly the moment a halt may be
            waiting. `error`, because a failed connection is a system failure,
            never `bearish`. Deliberately unlike the count badge — hollow, a
            glyph rather than a number, bottom corner rather than top — so a
            stale count and the marker beside it cannot be read as one thing,
            and the "!" carries it for anyone who cannot see the colour. */}
        {failed ? (
          <span
            aria-hidden="true"
            data-marker="unavailable"
            className="absolute bottom-1 right-1 flex h-3.5 w-3.5 items-center justify-center rounded-full border border-error bg-surface-container-lowest text-[10px] font-bold leading-none text-error"
          >
            !
          </span>
        ) : null}
      </button>

      {open ? (
        <div
          role="dialog"
          aria-label="Notifications"
          className="absolute right-0 z-50 mt-2 w-[22rem] rounded-lg border border-outline-warm bg-surface-container-lowest shadow-hover dark:shadow-none"
        >
          <div className="flex items-center justify-between gap-3 border-b border-outline-warm px-4 py-3">
            <h2 className="text-label-md uppercase tracking-wide text-on-surface-variant">
              Notifications
            </h2>
            {unreadIds.length > 0 ? (
              <button
                type="button"
                onClick={() => markRead.mutate(unreadIds)}
                disabled={markRead.isPending}
                className="rounded text-caption text-on-surface-variant underline decoration-outline-variant hover:text-on-surface disabled:cursor-not-allowed disabled:opacity-60"
              >
                Mark all read
              </button>
            ) : null}
          </div>

          {failedStale ? (
            <p role="status" className="border-b border-outline-variant px-4 py-2 text-caption text-error">
              The last refresh failed — this list may be out of date.
            </p>
          ) : null}
          {actionError ? (
            <div className="border-b border-outline-variant px-4 py-2 text-caption">
              <RequestFailed error={actionError} what="that change" />
            </div>
          ) : null}

          {query.isPending ? (
            /* Loading is a real condition, not a timer: until the first read
               lands there is no list, and "nothing yet" would be a claim. */
            <div role="status" aria-label="Loading notifications" className="space-y-3 px-4 py-4">
              {[0, 1, 2].map((i) => (
                <span
                  key={i}
                  aria-hidden="true"
                  className="block h-3 animate-pulse rounded bg-surface-container-high"
                />
              ))}
            </div>
          ) : failedEmpty ? (
            <div className="px-4 py-4">
              <RequestFailed error={query.error} what="notifications" />
              <p className="mt-2 text-caption text-on-surface-variant">
                Notifications are unavailable, not empty — engine faults and halts may be waiting
                here. The bell retries every 15 seconds.
              </p>
            </div>
          ) : visible.length === 0 ? (
            /* A designed empty state. "Nothing yet" in the morning and
               "nothing yet" at 3pm mean different things, so it says what the
               absence means rather than just that there is one. */
            <p className="px-4 py-6 text-caption text-on-surface-variant">
              Nothing yet. Fills, stops and engine faults arrive here as they happen — an empty bell
              during the session means the engine has had nothing to report, not that it is idle.
            </p>
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {visible.map((n) => {
                // The server's severity, stored as raised — never re-derived
                // from the event here, so history cannot be recoloured.
                const severity = n.severity
                const eventLabel = NOTIFICATION_EVENT_LABEL[n.event] ?? n.title
                return (
                  <li
                    key={n.id}
                    className={`flex items-start gap-3 border-b border-outline-variant px-4 py-3 last:border-0 ${
                      n.read ? '' : 'bg-surface-container-low'
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${SEVERITY_DOT_CLASS[severity]}`}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className={`text-label-md ${SEVERITY_CLASS[severity]}`}>
                          {n.title}
                        </span>
                        <time
                          dateTime={n.time}
                          title={formatDateTimeET(n.time)}
                          className="shrink-0 text-caption text-on-surface-variant"
                        >
                          {formatTimeET(n.time)}
                        </time>
                      </div>
                      <p className="mt-0.5 text-caption text-on-surface-variant">{n.detail}</p>
                      {!n.read ? <span className="sr-only">Unread</span> : null}
                    </div>
                    <button
                      type="button"
                      onClick={() => dismiss.mutate(n.id)}
                      aria-label={`Dismiss ${eventLabel}`}
                      className="shrink-0 rounded p-1 text-on-surface-variant hover:bg-surface-container-high hover:text-on-surface"
                    >
                      <XIcon className="h-3.5 w-3.5" />
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      ) : null}
    </div>
  )
}
