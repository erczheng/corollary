import { useEffect, useRef, useState } from 'react'
import { BellIcon, XIcon } from './icons'
import { useUIStore } from '../lib/store'
import {
  SEVERITY_CLASS,
  SEVERITY_DOT_CLASS,
  severityFor,
  unreadCount,
  visibleNotifications,
} from '../lib/notifications'
import { NOTIFICATION_EVENT_LABEL } from '../lib/mockData'
import { formatDateTimeET, formatTimeET } from '../lib/format'

const ICON_BUTTON =
  'flex h-9 w-9 items-center justify-center rounded transition-colors duration-base ease-standard'

const ICON_IDLE = 'text-on-surface-variant hover:bg-surface-container-low hover:text-on-surface'

/** The in-app bell of PRD.md §10 — the channel that receives every event the
 * routing matrix sends it.
 *
 * A panel anchored to the bell rather than a page. A notification you have to
 * navigate to is one you miss while watching a position, and PRD.md §11 does
 * not list a notifications page among the app's screens. If a history view is
 * ever wanted, the store already owns the list and this grows into a route
 * without rework.
 *
 * **Opening does not mark anything read.** Glancing at a badge and closing the
 * panel is not the same act as reading eight notifications, and a panel that
 * silently clears itself on open destroys the one piece of state that tells
 * you what you have not yet seen. Clearing is an explicit button.
 */
export function NotificationBell() {
  const accountMode = useUIStore((s) => s.accountMode)
  const notifications = useUIStore((s) => s.notifications)
  const markRead = useUIStore((s) => s.markNotificationsRead)
  const dismiss = useUIStore((s) => s.dismissNotification)

  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Scoped to the book on screen. A paper fill announcing itself while Cash is
  // live misreports which money moved — the same reason positions are keyed by
  // account. Engine-level events carry no account and appear in both.
  const visible = visibleNotifications(notifications, accountMode)
  const unread = unreadCount(notifications, accountMode)

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

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        // The count goes in the accessible name, not only in the badge — a
        // screen reader gets "3 unread" rather than a bare "Notifications"
        // beside a red dot it cannot see.
        aria-label={unread > 0 ? `Notifications — ${unread} unread` : 'Notifications'}
        aria-expanded={open}
        aria-haspopup="dialog"
        title={unread > 0 ? `${unread} unread` : 'Notifications — no unread'}
        className={`${ICON_BUTTON} ${open ? 'bg-surface-container-low text-on-surface' : ICON_IDLE}`}
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
            {unread > 0 ? (
              <button
                type="button"
                onClick={markRead}
                className="rounded text-caption text-on-surface-variant underline decoration-outline-variant hover:text-on-surface"
              >
                Mark all read
              </button>
            ) : null}
          </div>

          {visible.length === 0 ? (
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
                const severity = severityFor(n.event)
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
                          {NOTIFICATION_EVENT_LABEL[n.event]}
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
                      onClick={() => dismiss(n.id)}
                      aria-label={`Dismiss ${NOTIFICATION_EVENT_LABEL[n.event]}`}
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
