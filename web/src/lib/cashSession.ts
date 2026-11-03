/**
 * Rule 5's Paper/Cash switch, as the browser's half of a server-checked
 * session. No React in here; the toggle and the watch hook call these.
 *
 * The engine holds the confirmed Cash session in memory (`POST /api/account/
 * cash-session`), so **the store's `accountMode` follows the server, never
 * leads it** in the dangerous direction:
 *
 * - **Into Cash** only on a 200. A refusal (409 mismatch, 409 ended while
 *   confirming, 503 no live keys, 502 vendor) leaves the store in Paper and
 *   hands the server's own words back to the dialog.
 * - **Into Paper** at once, then `DELETE`. Paper is the safe direction and
 *   does not wait on a request; a failed `DELETE` is reported, not undone.
 * - **At boot, and then as a standing check**, `checkCashSession` re-reads
 *   the server. A session the engine holds while this tab is in Paper is
 *   *ended*, never adopted — a reload is a cold start and rule 5 requires
 *   re-confirmation. A tab in Cash whose session the engine no longer holds
 *   (it restarted, or another tab ended it) **drops to Paper at once** and
 *   says why. No check ever moves a tab into Cash.
 *
 * `cashSessionPending` is the one guard against both double submission and a
 * `DELETE` racing a `POST`: it is set synchronously before the request leaves,
 * so a second click in the same tick finds it already set.
 * `cashSessionReconciling` is its mirror: while a check's `GET`/`DELETE` is
 * in flight the confirm is refused, so a `DELETE` sent on a read taken before
 * the `POST` can never land after it. The two are mutually exclusive.
 *
 * **A consequence worth knowing:** any tab in Paper ends the engine's session
 * within one Paper interval, so a second tab left in Paper takes a Cash tab
 * back to Paper. That is fail-off by design — the engine holds one session,
 * and a tab that did not confirm it does not get to keep it alive.
 *
 * Nothing here touches storage. `theme.ts` is the only persisted state.
 */
import { confirmCashSession, endCashSession, fetchCashSession, isApiError, isUnreachable } from './api'
import { onLiveSocketOpen } from './liveSocket'
import { queryClient } from './queryClient'
import { useUIStore } from './store'
import type { AccountMode, CashSessionState, Notification } from './types'

/** How often the standing check re-reads while this tab is in Cash — the
 * longest a tab can show Cash over a session the engine has already ended,
 * absent a focus, reconnect or notification to cut it short. */
export const CASH_SESSION_CHECK_CASH_MS = 15_000
/** …and while in Paper, where the only job is ending a stray session. */
export const CASH_SESSION_CHECK_PAPER_MS = 60_000
/** A check that has not answered by now is abandoned — its request aborted,
 * its result discarded — and the Cash pill unlocked, so a hung engine cannot
 * hold the pill forever. */
export const CASH_SESSION_CHECK_TIMEOUT_MS = 10_000
/** First retry after an unreadable check; doubles, capped at the interval. */
export const CASH_SESSION_RETRY_BASE_MS = 2_000

/** What the toggle says, in `error`, after a check moved the tab to Paper. */
export const CASH_SESSION_DROPPED_MESSAGE =
  'The engine ended the Cash session; you are in Paper. Confirm again to use Cash.'

export function cashSessionCheckInterval(mode: AccountMode): number {
  return mode === 'cash' ? CASH_SESSION_CHECK_CASH_MS : CASH_SESSION_CHECK_PAPER_MS
}

/** Delay before the next check. `failures` counts consecutive unreadable
 * checks; zero is the plain interval. Never longer than the interval. */
export function cashSessionRetryDelay(mode: AccountMode, failures: number): number {
  const interval = cashSessionCheckInterval(mode)
  if (failures <= 0) return interval
  return Math.min(interval, CASH_SESSION_RETRY_BASE_MS * 2 ** (failures - 1))
}

export type CashConfirmOutcome =
  | { ok: true; session: CashSessionState }
  | { ok: false; code: string; message: string }

/** What a refused or failed request says to the operator — the server's own
 * message when it sent one, since it is written to be read. */
export function cashSessionErrorMessage(error: unknown): string {
  if (isUnreachable(error)) return 'The engine did not answer. Check that it is running.'
  if (isApiError(error)) return error.message
  return 'The request failed for an unknown reason.'
}

let checking = false
let recheckAfterConfirm = false

/** Test seam: forget module state between tests. */
export function resetCashSessionChecks(): void {
  checking = false
  recheckAfterConfirm = false
}

/** How long a confirm may hang before it is abandoned (the dialog it holds
 * open covers Halt and Flatten). The server's own live read is bounded at
 * about 15s; this sits just past that. */
export const CASH_SESSION_CONFIRM_TIMEOUT_MS = 20_000

/** Confirm Cash for the account the dialog displayed. Returns `null` when a
 * confirm is already in flight, or a check is — the click did nothing, by
 * design. */
export async function confirmCash(accountNumberMasked: string): Promise<CashConfirmOutcome | null> {
  const store = useUIStore.getState()
  if (store.cashSessionPending || store.cashSessionReconciling) return null
  store.setCashSessionPending(true)
  let ok = false
  let timedOut = false
  // The pending dialog covers the header's Halt and Flatten, so a confirm
  // that hangs is abandoned: the tab stays in Paper, and the check owed in
  // `finally` ends anything the server committed after all.
  const abort = new AbortController()
  const timer = setTimeout(() => {
    timedOut = true
    abort.abort()
  }, CASH_SESSION_CONFIRM_TIMEOUT_MS)
  try {
    const session = await confirmCashSession(accountNumberMasked, { signal: abort.signal })
    // A 200 that does not say active is not a confirmation -- nor is one
    // without the confirmedAt this tab must remember to recognise its own.
    if (!session?.active || !session.confirmedAt) {
      return {
        ok: false,
        code: 'cash_session_inactive',
        message: 'The engine answered without confirming a Cash session.',
      }
    }
    const after = useUIStore.getState()
    after.setCashSessionEndError(null)
    after.setCashSessionDropped(null)
    after.setCashSessionConfirmedAt(session.confirmedAt)
    after.setAccountMode('cash')
    ok = true
    return { ok: true, session }
  } catch (error) {
    if (timedOut) {
      recheckAfterConfirm = true
      return {
        ok: false,
        code: 'cash_session_confirm_timeout',
        message: 'The engine did not answer in time. You are still in Paper; confirm again if Cash is wanted.',
      }
    }
    return {
      ok: false,
      code: isApiError(error) ? error.code : 'unknown',
      message: cashSessionErrorMessage(error),
    }
  } finally {
    clearTimeout(timer)
    useUIStore.getState().setCashSessionPending(false)
    // A check skipped because this confirm was in flight still owes its
    // answer: a refused confirm can leave behind the session it would have
    // ended. A successful one is newer than anything that check read.
    const owed = recheckAfterConfirm
    recheckAfterConfirm = false
    if (!ok && owed) void checkCashSession()
  }
}

function endFailedMessage(error: unknown): string {
  return `You are in Paper, but the engine did not confirm it ended the Cash session: ${cashSessionErrorMessage(error)}`
}

/** Tell the engine the Cash session is over. Exported for the toggle's retry. */
export async function endCash(): Promise<boolean> {
  try {
    await endCashSession()
    useUIStore.getState().setCashSessionEndError(null)
    return true
  } catch (error) {
    useUIStore.getState().setCashSessionEndError(endFailedMessage(error))
    return false
  }
}

/** Switch to Paper: the store first, then the `DELETE`.
 *
 * Ignored while a confirm is pending — the toggle is disabled then, and this
 * is the second line. Only a switch *out of* Cash sends the `DELETE`; clicking
 * Paper while already in Paper is not an event. */
export async function switchToPaper(): Promise<void> {
  const store = useUIStore.getState()
  if (store.cashSessionPending) return
  const wasCash = store.accountMode === 'cash'
  store.setAccountMode('paper')
  store.setCashSessionConfirmedAt(null)
  if (wasCash) await endCash()
}

export type CashSessionCheckResult =
  /** Paper here, none on the server. */
  | 'none'
  /** Cash here, and the server holds the session this tab confirmed. */
  | 'agrees'
  /** Paper here, a session on the server: ended. */
  | 'ended'
  | 'end_failed'
  /** Cash here, and the session this tab confirmed is gone: now Paper. */
  | 'dropped'
  /** A confirm was in flight; the check re-runs if that confirm fails. */
  | 'skipped'
  /** Another check is already running. */
  | 'in_flight'
  /** No answer, an error, or the timeout. Changes nothing; retried. */
  | 'unreadable'

/** Read the engine's Cash session and make this tab agree with it — **only
 * ever in the safe direction**.
 *
 * - Paper here, active there: `DELETE`. A session kept across a reload or
 *   left by another tab is ended, never adopted.
 * - Cash here, inactive there — or active under a `confirmedAt` that is not
 *   the one this tab confirmed: **drop to Paper now** and state why. That is
 *   the engine restarting under an open tab, or another tab ending it.
 * - Never Paper → Cash. Nothing in this function calls
 *   `setAccountMode('cash')`, and `cashSession.test.ts` pins it.
 *
 * Holds `cashSessionReconciling` across the `GET` and the `DELETE`, under a
 * 10s timeout that aborts the request and discards its result. `unreadable`
 * changes nothing: an engine that does not answer is reported by every page
 * that needs it, and the watch retries. */
export async function checkCashSession(): Promise<CashSessionCheckResult> {
  if (checking) return 'in_flight'
  if (useUIStore.getState().cashSessionPending) {
    recheckAfterConfirm = true
    return 'skipped'
  }
  checking = true
  useUIStore.getState().setCashSessionReconciling(true)
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
    useUIStore.getState().setCashSessionReconciling(false)
  }, CASH_SESSION_CHECK_TIMEOUT_MS)
  try {
    let state: CashSessionState
    try {
      state = await fetchCashSession({ signal: controller.signal })
    } catch {
      return 'unreadable'
    }
    if (timedOut) return 'unreadable'
    const store = useUIStore.getState()
    if (store.accountMode === 'cash') {
      const mine = store.cashSessionConfirmedAt
      const held = state?.active === true && mine !== null && state.confirmedAt === mine
      if (held) return 'agrees'
      store.setAccountMode('paper')
      store.setCashSessionConfirmedAt(null)
      store.setCashSessionDropped(CASH_SESSION_DROPPED_MESSAGE)
      return 'dropped'
    }
    if (!state?.active) return 'none'
    try {
      await endCashSession({ signal: controller.signal })
    } catch (error) {
      if (timedOut) return 'unreadable'
      useUIStore.getState().setCashSessionEndError(endFailedMessage(error))
      return 'end_failed'
    }
    useUIStore.getState().setCashSessionEndError(null)
    return 'ended'
  } finally {
    clearTimeout(timer)
    checking = false
    useUIStore.getState().setCashSessionReconciling(false)
  }
}

/** The boot reconcile is the first standing check. The name stays because
 * "at boot" is the case rule 5 is written about. */
export const reconcileCashSessionAtBoot = checkCashSession

/** Ids of `cash_session_ended` notifications in `list` not yet in `seen`. */
export function unseenCashSessionEnded(
  list: readonly Notification[] | undefined,
  seen: ReadonlySet<string>,
): string[] {
  if (!Array.isArray(list)) return []
  return list.filter((n) => n.event === 'cash_session_ended' && !seen.has(n.id)).map((n) => n.id)
}

/** Start the standing check. Returns the stop.
 *
 * Checks at once (the boot reconcile), then again:
 * - on a timer — `CASH_SESSION_CHECK_CASH_MS` in Cash, `…_PAPER_MS` in
 *   Paper; after an `unreadable`, sooner, backing off from
 *   `CASH_SESSION_RETRY_BASE_MS` up to that interval;
 * - when the window regains focus or the page becomes visible;
 * - each time the live socket opens — a reconnect may be a restarted engine;
 * - when a `cash_session_ended` notification reaches the bell's cache.
 *
 * Every trigger runs the same `checkCashSession`, so none can do more than
 * the safe direction it allows. */
export function startCashSessionWatch(): () => void {
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let failures = 0

  const schedule = () => {
    if (stopped) return
    if (timer !== null) clearTimeout(timer)
    timer = setTimeout(run, cashSessionRetryDelay(useUIStore.getState().accountMode, failures))
  }

  function run() {
    if (stopped) return
    void checkCashSession()
      .catch(() => 'unreadable' as const)
      .then((result) => {
      if (stopped) return
      if (result === 'unreadable') failures += 1
      else if (result !== 'in_flight') failures = 0
      schedule()
    })
  }

  const onFocus = () => run()
  const onVisibility = () => {
    if (document.visibilityState === 'visible') run()
  }
  window.addEventListener('focus', onFocus)
  document.addEventListener('visibilitychange', onVisibility)
  const offSocket = onLiveSocketOpen(run)

  // Entering or leaving Cash changes the cadence, not the check.
  const offMode = useUIStore.subscribe((state, prev) => {
    if (state.accountMode !== prev.accountMode) schedule()
  })

  // Seeded from what is already cached: the watch reacts to notifications
  // that arrive, not to history the bell already held.
  const cache = queryClient.getQueryCache()
  const seen = new Set<string>()
  for (const query of cache.findAll({ queryKey: ['notifications'] })) {
    for (const id of unseenCashSessionEnded(query.state.data as Notification[] | undefined, seen)) {
      seen.add(id)
    }
  }
  const offCache = cache.subscribe((event) => {
    if (event.type !== 'updated' || event.query.queryKey[0] !== 'notifications') return
    const fresh = unseenCashSessionEnded(event.query.state.data as Notification[] | undefined, seen)
    if (fresh.length === 0) return
    for (const id of fresh) seen.add(id)
    run()
  })

  run()

  return () => {
    stopped = true
    if (timer !== null) clearTimeout(timer)
    window.removeEventListener('focus', onFocus)
    document.removeEventListener('visibilitychange', onVisibility)
    offSocket()
    offMode()
    offCache()
  }
}
