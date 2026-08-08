import { useState } from 'react'
import { useUIStore } from '../lib/store'
import { ConfirmDialog } from './ConfirmDialog'

/** Paper / Cash switch. Paper is the default on every cold start; entering
 * Cash requires an explicit confirm naming the account and balance
 * (CLAUDE.md rule 5). The account/balance values are placeholders until
 * Phase 2 wires up the Alpaca account endpoint — this dialog exists now so
 * the confirm step is never bolted on later as an afterthought. */
export function AccountModeToggle() {
  const accountMode = useUIStore((s) => s.accountMode)
  const setAccountMode = useUIStore((s) => s.setAccountMode)
  const [confirming, setConfirming] = useState(false)

  return (
    <>
      <div className="flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md">
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
            Orders placed from here on use real money in the account
            connected to your live Alpaca keys. Balance not yet available —
            the Account page connects in Phase 2.
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
