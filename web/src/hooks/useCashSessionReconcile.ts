import { useEffect } from 'react'
import { startCashSessionWatch } from '../lib/cashSession'

/** Rule 5's standing Cash-session check, mounted once at the app shell.
 *
 * A browser reload is a cold start: the store comes up in Paper, and if the
 * engine still holds a Cash session from before the reload, the first check
 * ends it so the two agree. After that it keeps checking — on an interval,
 * on focus, on a socket reconnect, and on a `cash_session_ended`
 * notification — so a tab in Cash whose session the engine dropped (a
 * restart, another tab) falls back to Paper rather than showing Cash over
 * nothing. It never moves a tab into Cash. See `startCashSessionWatch`.
 *
 * Registers its cleanup, unlike `useLiveSocket`: StrictMode's remount stops
 * the first watch and starts a second, and the second's opening check finds
 * the first's still in flight (`in_flight`), so one `GET` is sent, not two. */
export function useCashSessionReconcile(): void {
  useEffect(() => startCashSessionWatch(), [])
}
