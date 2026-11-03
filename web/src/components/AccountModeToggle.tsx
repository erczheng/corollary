import { useState } from 'react'
import { useUIStore } from '../lib/store'
import { isAccountUnavailable, isUnreachable } from '../lib/api'
import { useCashConfirmAccount } from '../lib/queries'
import { confirmCash, endCash, switchToPaper } from '../lib/cashSession'
import { cashOutsideBuyingPower } from '../lib/account'
import { formatUsd } from '../lib/format'
import type { AccountResponse } from '../lib/types'
import { ConfirmDialog } from './ConfirmDialog'

/** Paper / Cash switch. Paper is the default on every cold start; entering
 * Cash requires an explicit confirm naming the account **and its balance**
 * (PRD.md §2, CLAUDE.md rule 5) — and the engine has to agree.
 *
 * **The dialog names the real live account.** It reads `GET /api/account?
 * account=cash` when it opens and quotes the masked account number and the
 * live equity. Confirming posts that same masked string back to `POST
 * /api/account/cash-session`; only a 200 switches the store (see
 * `cashSession.ts`). While the account is loading, unreadable, or reports no
 * number, there is nothing to confirm and the confirm stays disabled — a
 * dialog that let you confirm an account it could not name would be
 * confirming nothing.
 *
 * **Buying power is named beside the balance**, and options buying power
 * beside that — options are not marginable, so it is the figure an options
 * order is actually bound by. Cash the broker does not count in buying power
 * is stated as an amount with no reason attached, because Alpaca reports
 * buying power without reporting what it holds back. An absent options
 * figure is said in words, never shown as $0.
 *
 * **While a Cash-session check is in flight** (`cashSessionReconciling`),
 * the Cash pill and the confirm are disabled, so the operator's `POST`
 * cannot be overtaken by a check's `DELETE`. The Paper pill stays live —
 * leaving Cash never waits. A check that moved this tab out of Cash says so
 * here, in `error`.
 *
 * Refusals render in `error`, not `bearish`: a refused operator action is a
 * rule outcome, not a loss. */
export function AccountModeToggle() {
  const accountMode = useUIStore((s) => s.accountMode)
  const pending = useUIStore((s) => s.cashSessionPending)
  const endError = useUIStore((s) => s.cashSessionEndError)
  const reconciling = useUIStore((s) => s.cashSessionReconciling)
  const dropped = useUIStore((s) => s.cashSessionDropped)
  const [confirming, setConfirming] = useState(false)
  const [refusal, setRefusal] = useState<string | null>(null)
  const live = useCashConfirmAccount(confirming)

  const masked = live.data?.accountNumberMasked ?? null
  const canConfirm = confirming && live.isSuccess && !live.isFetching && masked !== null && !pending && !reconciling

  const close = () => {
    if (pending) return
    setConfirming(false)
    setRefusal(null)
  }

  const onConfirm = async () => {
    if (!canConfirm || masked === null) return
    setRefusal(null)
    const outcome = await confirmCash(masked)
    if (outcome === null) return
    if (outcome.ok) {
      setConfirming(false)
      return
    }
    setRefusal(outcome.message)
    // A mismatch means the number on screen is not the live account's any
    // more; re-read it so a retry names what is actually there.
    void live.refetch()
  }

  // A pending confirm greys the pills out; a check holds the Cash pill for a
  // few milliseconds every interval, and greying it each time would flicker.
  const pillClass = (selected: boolean) =>
    (selected
      ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
      : 'rounded-full px-3 py-1 text-on-surface-variant') +
    (pending ? ' disabled:cursor-not-allowed disabled:opacity-50' : ' disabled:cursor-progress')

  return (
    <>
      <div className="flex flex-col items-end gap-1">
        <div
          className="flex rounded-full border border-outline bg-surface-container-low p-1 text-label-md"
          aria-busy={pending}
        >
          <button
            type="button"
            onClick={() => void switchToPaper()}
            aria-pressed={accountMode === 'paper'}
            disabled={pending}
            className={pillClass(accountMode === 'paper')}
          >
            Paper
          </button>
          <button
            type="button"
            onClick={() => {
              if (accountMode === 'cash') return
              setRefusal(null)
              setConfirming(true)
            }}
            aria-pressed={accountMode === 'cash'}
            disabled={pending || reconciling}
            className={pillClass(accountMode === 'cash')}
          >
            Cash
          </button>
        </div>
        {dropped !== null && accountMode === 'paper' ? (
          <p role="alert" className="max-w-xs text-right text-caption text-error">
            {dropped}
          </p>
        ) : null}
        {endError !== null ? (
          <p role="alert" className="max-w-xs text-right text-caption text-error">
            {endError}{' '}
            <button
              type="button"
              onClick={() => void endCash()}
              className="rounded underline"
            >
              Try again
            </button>
          </p>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirming}
        title="Switch to Cash account?"
        consequence={<Consequence account={live.data ?? null} ready={live.isSuccess} />}
        confirmLabel={pending ? 'Confirming with the engine…' : 'Switch to Cash'}
        confirmDisabled={!canConfirm}
        cancelDisabled={pending}
        onConfirm={() => void onConfirm()}
        onCancel={close}
      >
        <DialogStatus
          loading={live.isFetching}
          reconciling={reconciling}
          error={live.isError ? live.error : null}
          unnamed={live.isSuccess && masked === null}
          refusal={refusal}
          onRetry={() => void live.refetch()}
        />
      </ConfirmDialog>
    </>
  )
}

function Consequence({ account, ready }: { account: AccountResponse | null; ready: boolean }) {
  if (!ready || account === null || account.accountNumberMasked === null) {
    return <>Orders placed from here on would use real money in the account connected to your live Alpaca keys.</>
  }
  const gap = cashOutsideBuyingPower(account.cash, account.buyingPower)
  const num = 'font-mono tabular-nums text-on-surface'
  return (
    <>
      Orders placed from here on use real money in the live Alpaca account{' '}
      <span className={num}>{account.accountNumberMasked}</span> — balance{' '}
      <span className={num}>{formatUsd(account.equity)}</span>, cash{' '}
      <span className={num}>{formatUsd(account.cash)}</span>
      {gap > 0 ? (
        <>
          , of which <span className={num}>{formatUsd(gap)}</span> is not counted in buying
          power
        </>
      ) : null}
      . Buying power <span className={num}>{formatUsd(account.buyingPower)}</span>;{' '}
      {account.optionsBuyingPower !== null ? (
        <>
          options buying power{' '}
          <span className={num}>{formatUsd(account.optionsBuyingPower)}</span>, the figure an
          options order is bound by.
        </>
      ) : (
        <>the broker did not report options buying power, so it cannot be quoted here.</>
      )}
    </>
  )
}

function DialogStatus({
  loading,
  reconciling,
  error,
  unnamed,
  refusal,
  onRetry,
}: {
  loading: boolean
  reconciling: boolean
  error: unknown
  unnamed: boolean
  refusal: string | null
  onRetry: () => void
}) {
  if (refusal !== null) {
    return (
      <p role="alert" className="mt-3 text-body-md text-error">
        {refusal} You are still in Paper.
      </p>
    )
  }
  if (error !== null) {
    const why = isUnreachable(error)
      ? 'The engine did not answer, so the live account could not be read.'
      : isAccountUnavailable(error)
        ? `Cash is not configured: ${(error as Error).message}`
        : `The live account could not be read: ${(error as Error).message}`
    return (
      <div role="alert" className="mt-3 text-body-md text-error">
        <p>{why} Cash cannot be confirmed until it can.</p>
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded border border-outline px-3 py-1 text-label-md text-on-surface"
        >
          Read the account again
        </button>
      </div>
    )
  }
  if (unnamed) {
    return (
      <p role="alert" className="mt-3 text-body-md text-error">
        The live account reported no account number, so there is nothing to confirm against.
        Cash cannot be confirmed.
      </p>
    )
  }
  if (reconciling) {
    return (
      <p role="status" className="mt-3 text-body-md text-on-surface-variant">
        Checking the engine&apos;s Cash session…
      </p>
    )
  }
  if (loading) {
    return (
      <p role="status" className="mt-3 text-body-md text-on-surface-variant">
        Reading the live account from Alpaca…
      </p>
    )
  }
  return null
}
