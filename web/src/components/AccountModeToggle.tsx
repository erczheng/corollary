import { useState } from 'react'
import { useUIStore } from '../lib/store'
import { ACCOUNT_SNAPSHOTS } from '../lib/mockData'
import { formatUsd } from '../lib/format'
import { ConfirmDialog } from './ConfirmDialog'

/** Paper / Cash switch. Paper is the default on every cold start; entering
 * Cash requires an explicit confirm naming the account **and its balance**
 * (PRD.md §2, CLAUDE.md rule 5).
 *
 * **Buying power leads, not cash.** They differ on this account — a cash
 * account has no margin and so can spend less than its balance, which makes
 * quoting the cash figure alone in the dialog that precedes real trading an
 * overstatement of what can actually be deployed. Cash is named beside it so
 * the gap is visible rather than silently resolved in either direction; it is
 * the same distinction the Account page exists to make. The dialog states the
 * gap and not its cause, because Alpaca reports buying power without
 * reporting what it is holding back.
 *
 * Phase 2 swaps `ACCOUNT_SNAPSHOTS` for the Alpaca account endpoint and
 * changes nothing here. */
export function AccountModeToggle() {
  const accountMode = useUIStore((s) => s.accountMode)
  const setAccountMode = useUIStore((s) => s.setAccountMode)
  const [confirming, setConfirming] = useState(false)

  // The book being switched *into*, never the one on screen — the whole
  // point of the dialog is to state what you are about to start trading.
  const cash = ACCOUNT_SNAPSHOTS.cash

  return (
    <>
      <div className="flex rounded-full border border-outline bg-surface-container-low p-1 text-label-md">
        <button
          type="button"
          onClick={() => setAccountMode('paper')}
          aria-pressed={accountMode === 'paper'}
          className={
            accountMode === 'paper'
              ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
              : 'rounded-full px-3 py-1 text-on-surface-variant'
          }
        >
          Paper
        </button>
        <button
          type="button"
          onClick={() => setConfirming(true)}
          aria-pressed={accountMode === 'cash'}
          className={
            accountMode === 'cash'
              ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
              : 'rounded-full px-3 py-1 text-on-surface-variant'
          }
        >
          Cash
        </button>
      </div>
      <ConfirmDialog
        open={confirming}
        title="Switch to Cash account?"
        consequence={
          <>
            Orders placed from here on use real money in the account connected to your live Alpaca
            keys — buying power {formatUsd(cash.buyingPower)}, of {formatUsd(cash.cash)} cash.
            {cash.cash > cash.buyingPower ? (
              <>
                {' '}
                The {formatUsd(cash.cash - cash.buyingPower)} difference is yours but not spendable
                yet.
              </>
            ) : null}
          </>
        }
        confirmLabel="Switch to Cash"
        onConfirm={() => {
          setAccountMode('cash')
          setConfirming(false)
        }}
        onCancel={() => setConfirming(false)}
      />
    </>
  )
}
