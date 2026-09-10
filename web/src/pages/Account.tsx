import type { ReactNode } from 'react'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { LiveStatus } from '../components/LiveStatus'
import { StatCard } from '../components/StatCard'
import { ValueSkeleton } from '../components/Skeleton'
import { BankIcon, TargetIcon, TrendingUpIcon } from '../components/icons'
import { useLiveTick } from '../hooks/useLiveTick'
import { useUIStore } from '../lib/store'
import { ACCOUNT_LABEL, ACCOUNT_SNAPSHOTS, ACTIVITY_ACTION_LABEL, ACTIVITY_STATUS_CLASS, ACTIVITY_STATUS_LABEL } from '../lib/mockData'
import { cashTransfers, netTransfers, positionsValue, totalEquity } from '../lib/account'
import { formatDateTimeET, formatUsd, signClass } from '../lib/format'

/** Where the Alpaca dashboard's transfers live.
 *
 * Deliberately the dashboard root rather than a guessed deep link: PRD.md
 * §8.6 says deposits and withdrawals link *out*, and a confidently wrong
 * path is worse than one extra click. Phase 2 confirms the real one against
 * a live account — paper accounts have no banking page at all, which is
 * itself a reason not to hardcode one now. */
const ALPACA_URL = 'https://app.alpaca.markets/'

/** Matches Activity's stream rate. The same feed drives both — this page
 * subscribes because position value is marked from it, not because anything
 * here needs a faster clock than the positions screen. */
const TICK_MS = 400

const TH = 'whitespace-nowrap px-3 py-1 text-caption uppercase tracking-wide text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'

/** A panel on this page. `region` so the tests and a screen reader can tell
 * the three apart, the same way the Markets and News sections are built. */
function Panel({
  id,
  title,
  aside,
  children,
}: {
  id: string
  title: string
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <section
      aria-labelledby={id}
      className="rounded-lg border border-outline-warm bg-surface-container-lowest p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={id} className="text-label-md uppercase tracking-wide text-on-surface-variant">
          {title}
        </h2>
        {aside}
      </div>
      <div className="mt-4">{children}</div>
    </section>
  )
}

/** One line of the reconciliation. `total` gets the rule above it and the
 * heavier weight — the arithmetic should be readable as arithmetic. */
function Line({
  label,
  value,
  note,
  total = false,
}: {
  label: string
  value: ReactNode
  note?: string
  total?: boolean
}) {
  return (
    <div
      className={`flex items-baseline justify-between gap-4 py-2 ${
        total ? 'mt-1 border-t border-outline-warm pt-3' : ''
      }`}
    >
      <span className={total ? 'text-body-md text-on-surface' : 'text-body-md text-on-surface-variant'}>
        {label}
        {note ? <span className="ml-2 text-caption text-on-surface-variant">{note}</span> : null}
      </span>
      <span className={total ? 'text-data-lg text-on-surface' : 'text-data-md text-on-surface'}>
        {value}
      </span>
    </div>
  )
}

/** PRD.md §8.6 — replaces "Wallet". **Alpaca is the source of truth and
 * Corollary keeps no ledger**, which is why nothing on this page is a running
 * balance the app maintains: the four figures are the broker's, and the two
 * derived numbers are sums of them.
 *
 * Carries the Paper/Cash switch, not just the header's read-only badge.
 * PRD.md §2 gives the switch to "any page whose entire contents are
 * account-scoped", and that is this page more completely than any other —
 * every number on it belongs to one account. (The §2 enumeration names only
 * Dashboard and Activity because it predates this page existing.)
 */
export function Account() {
  const accountMode = useUIStore((s) => s.accountMode)
  const positions = useUIStore((s) => s.openPositions[s.accountMode])
  const activity = useUIStore((s) => s.activity[s.accountMode])
  const lastTickAt = useUIStore((s) => s.lastTickAt)

  // Same stream Activity runs. Position value is marked from it, so the
  // equity figure below is live rather than a snapshot of when the page
  // loaded — and without subscribing here it would sit frozen.
  useLiveTick(TICK_MS)

  const snapshot = ACCOUNT_SNAPSHOTS[accountMode]
  const transfers = cashTransfers(activity)
  const held = positionsValue(positions)
  const equity = totalEquity(snapshot.cash, positions)
  const net = netTransfers(transfers)

  const isCashAccount = accountMode === 'cash'
  // Position marks come from the stream; cash does not. Until the first price
  // arrives there is no honest equity figure to print.
  const marked = lastTickAt !== null

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-display-lg text-on-surface">Account</h1>
        <AccountModeToggle />
      </div>

      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Alpaca is the source of truth for every figure here — Corollary keeps no ledger of its own.
        Showing the {ACCOUNT_LABEL[accountMode]} account.
      </p>

      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard label="Cash" value={formatUsd(snapshot.cash)} icon={<BankIcon />} />
        <StatCard
          label="Buying power"
          value={formatUsd(snapshot.buyingPower)}
          icon={<TrendingUpIcon />}
          // Two genuinely different figures, and the reason differs by
          // account: margin doubles it on Paper, while a Cash account has
          // none and so can spend less than its balance.
          note={isCashAccount ? 'Cash account — no margin' : 'Margin account — 2× cash'}
        />
        <StatCard
          label="Options buying power"
          value={formatUsd(snapshot.optionsBuyingPower)}
          icon={<TargetIcon />}
          note="Options are not marginable"
        />
      </div>

      <div className="mt-4">
        <Panel
          id="equity-heading"
          title="Total equity"
          // The pill sits on this panel rather than in the page title
          // because this is the only panel that moves. Cash and buying power
          // are static balances from the broker; position value is marked
          // from the stream. A "Live" badge over the whole page would be
          // claiming more than is true.
          aside={<LiveStatus />}
        >
          <Line label="Cash" value={formatUsd(snapshot.cash)} />
          <Line
            label="Open positions"
            note={`${positions.length} held`}
            value={
              marked ? (
                formatUsd(held)
              ) : (
                <ValueSkeleton label="Marking open positions" className="w-28" />
              )
            }
          />
          <Line
            label="Total equity"
            value={
              marked ? formatUsd(equity) : <ValueSkeleton label="Computing equity" className="w-32" />
            }
            total
          />

          <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
            Position value is the contracts' market value, marked from the price stream. Cash is the
            full balance — some of it may not be spendable yet, and buying power above is what is.
          </p>
        </Panel>
      </div>

      <div className="mt-4">
        <Panel
          id="transfers-heading"
          title="Cash transfers"
          aside={
            <a
              href={ALPACA_URL}
              target="_blank"
              rel="noreferrer"
              className="rounded text-label-md text-on-surface-variant underline decoration-outline-variant hover:text-on-surface"
            >
              Deposit or withdraw at Alpaca ↗
            </a>
          }
        >
          {transfers.length === 0 ? (
            /* A designed empty state, not a blank table. "No transfers" in a
               book that has traded means something specific: the balance got
               here through P&L rather than through funding. */
            <p className="max-w-prose text-body-md text-on-surface-variant">
              No deposits or withdrawals in the {ACCOUNT_LABEL[accountMode]} account. Its balance
              has moved through trading alone. Transfers are initiated at Alpaca, not here.
            </p>
          ) : (
            <>
              <table className="w-full border-collapse">
                <caption className="sr-only">
                  Deposits and withdrawals in the {ACCOUNT_LABEL[accountMode]} account
                </caption>
                <thead>
                  <tr className="border-b border-outline">
                    <th scope="col" className={`${TH} text-left`}>
                      Date
                    </th>
                    <th scope="col" className={`${TH} text-left`}>
                      Type
                    </th>
                    <th scope="col" className={`${TH} text-right`}>
                      Amount
                    </th>
                    <th scope="col" className={`${TH} text-left`}>
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {transfers.map((t) => (
                    <tr key={t.id} className="border-b border-outline-variant last:border-0">
                      <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                        {formatDateTimeET(t.time)}
                      </td>
                      <td className={`${TD} text-body-md text-on-surface`}>
                        {ACTIVITY_ACTION_LABEL[t.action]}
                      </td>
                      {/* Signed textually as well as by colour — DESIGN.md
                          requires the sign on every value, because colour
                          alone fails in grayscale and for colourblind
                          readers. `signClass` gives bullish/bearish, never
                          `error`: a withdrawal is not a failure. */}
                      <td className={`${TD} text-right text-data-md ${signClass(t.amount ?? 0)}`}>
                        {formatUsd(t.amount ?? 0, { signed: true })}
                      </td>
                      <td className={`${TD} text-label-md ${ACTIVITY_STATUS_CLASS[t.status]}`}>
                        {ACTIVITY_STATUS_LABEL[t.status]}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-outline-warm">
                    <td className={`${TD} text-label-md text-on-surface`} colSpan={2}>
                      Net transferred
                    </td>
                    <td className={`${TD} text-right text-data-md ${signClass(net)}`}>
                      {formatUsd(net, { signed: true })}
                    </td>
                    <td className={TD} />
                  </tr>
                </tfoot>
              </table>

              {!isCashAccount && (
                <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
                  Paper transfers are simulated by Alpaca — no money moved.
                </p>
              )}
            </>
          )}
        </Panel>
      </div>
    </div>
  )
}
