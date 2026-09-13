import type { ReactNode } from 'react'
import { AccountModeToggle } from '../components/AccountModeToggle'
import { RefreshButton } from '../components/RefreshButton'
import { RequestFailed } from '../components/RequestFailed'
import { StatCard } from '../components/StatCard'
import { StatCardSkeleton, TableSkeleton, ValueSkeleton } from '../components/Skeleton'
import { BankIcon, TargetIcon, TrendingDownIcon, TrendingUpIcon } from '../components/icons'
import { useAccount, usePositions, useTransfers } from '../lib/queries'
import { isAccountUnavailable, isApiError } from '../lib/api'
import { useUIStore } from '../lib/store'
import {
  ACCOUNT_LABEL,
  ACTIVITY_ACTION_LABEL,
  ACTIVITY_STATUS_CLASS,
  ACTIVITY_STATUS_LABEL,
} from '../lib/types'
import type { AccountMode, AccountResponse, ActivityItem, Page } from '../lib/types'
import { cashTransfers, netTransfers } from '../lib/account'
import { formatDateTimeET, formatTimeET, formatUsd, signClass } from '../lib/format'

/** Where the Alpaca dashboard's transfers live.
 *
 * Deliberately the dashboard root rather than a guessed deep link: PRD.md
 * §8.6 says deposits and withdrawals link *out*, and a confidently wrong
 * path is worse than one extra click. A paper account has no banking page at
 * all, which is itself a reason not to hardcode one. */
const ALPACA_URL = 'https://app.alpaca.markets/'

/** One request covers the whole ledger on any account a human funds by hand.
 * Above this the page says it is showing the most recent N of `total` rather
 * than letting the footer quietly net a page instead of a book. */
const TRANSFER_PAGE_SIZE = 100

const TH = 'whitespace-nowrap px-3 py-1 text-label-sm uppercase text-on-surface-variant'
const TD = 'px-3 py-1 align-middle'

/** A panel on this page. `region` so the tests and a screen reader can tell
 * them apart, the same way the Markets and News sections are built. */
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
  detail,
  total = false,
}: {
  label: string
  value: ReactNode
  note?: string
  detail?: ReactNode
  total?: boolean
}) {
  return (
    <div
      className={`flex items-baseline justify-between gap-4 py-2 ${
        total ? 'mt-1 border-t border-outline-warm pt-3' : ''
      }`}
    >
      <span className="text-body-md">
        <span className={total ? 'text-on-surface' : 'text-on-surface-variant'}>{label}</span>
        {note ? <span className="ml-2 text-caption text-on-surface-variant">{note}</span> : null}
        {detail ? <span className="block text-caption text-on-surface-variant">{detail}</span> : null}
      </span>
      <span className={total ? 'text-data-lg text-on-surface' : 'text-data-md text-on-surface'}>
        {value}
      </span>
    </div>
  )
}

/** The 409 the server answers `?account=cash` with while the live keys are
 * absent, rendered as the page rather than as a toast.
 *
 * This is the whole reason that status code exists: a `$0.00` equity where
 * the truth is "we cannot see this book" misreports real money. It is not an
 * error state either — nothing failed, the account is simply not configured
 * — so it renders in `caution`, not `error`.
 *
 * The variable **names** come from the paper response, which answers on every
 * account and carries `missingLiveCredentialEnvVars`. Names only, never
 * values: rule 6 says the UI shows masked presence, and a name is not a mask
 * of anything. */
function CashNotConfigured({
  reason,
  missing,
}: {
  reason: string
  missing: readonly string[]
}) {
  return (
    <section
      aria-labelledby="cash-unavailable-heading"
      className="mt-8 rounded-lg border border-caution bg-caution-container p-6"
    >
      <h2 id="cash-unavailable-heading" className="text-headline-md text-on-caution-container">
        Cash is not configured
      </h2>
      <p className="mt-3 max-w-prose text-body-md text-on-caution-container">{reason}</p>
      {missing.length > 0 && (
        <p className="mt-3 max-w-prose text-body-md text-on-caution-container">
          Missing from the environment:{' '}
          <span className="text-data-md">{missing.join(', ')}</span>. Set them in{' '}
          <span className="text-data-md">.env</span> and restart the engine. Corollary reads the
          values; it never displays them.
        </p>
      )}
      <p className="mt-3 max-w-prose text-caption text-on-caution-container">
        Paper is still readable — switch back above. No balance is shown here because there is none
        to read, not because the account is empty.
      </p>
    </section>
  )
}

/** Options buying power, or an honest absence.
 *
 * The broker's account object makes the field optional and it is the figure
 * an options trader is actually bound by — options are not marginable, so the
 * margin figure never binds them. A substituted number would overstate
 * capacity exactly the way the hardcoded "2×" sentence did. */
function optionsBuyingPowerValue(account: AccountResponse): ReactNode {
  if (account.optionsBuyingPower === null) {
    return <span className="text-body-md text-on-surface-variant">Not reported</span>
  }
  return formatUsd(account.optionsBuyingPower)
}

/** PRD.md §8.6 — replaces "Wallet". **Alpaca is the source of truth and
 * Corollary keeps no ledger**, which is why nothing on this page is a running
 * balance the app maintains.
 *
 * Every figure here now comes from `GET /api/account`, which reads
 * `GET /v2/account` and computes the reconciliation server-side. Client-side
 * money arithmetic is display-only: where the server publishes a figure —
 * `equity`, `netPositionValue`, `dayChange`, `derivedEquity` — the page reads
 * it rather than re-deriving it, because two derivations that disagreed would
 * have no way to say which one is right. The one sum still done here is the
 * net of the transfers actually on screen, and it is labelled as that.
 *
 * Carries the Paper/Cash switch, not just the header's read-only badge.
 * PRD.md §2 gives the switch to "any page whose entire contents are
 * account-scoped", and that is this page more completely than any other.
 */
export function Account() {
  const accountMode = useUIStore((s) => s.accountMode)

  const accountQuery = useAccount()
  const positionsQuery = usePositions()
  const transfersQuery = useTransfers({ pageSize: TRANSFER_PAGE_SIZE })
  // Paper answers whatever is selected, and its response is what carries
  // `cashAccountUnavailableReason` and the missing variable names. When Paper
  // *is* selected this is the same query key as `accountQuery` — one request,
  // not two.
  const paperQuery = useAccount('paper')

  const account = accountQuery.data
  const isCashAccount = accountMode === 'cash'

  const header = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-display-lg text-on-surface">Account</h1>
        <AccountModeToggle />
      </div>

      <p className="mt-2 max-w-prose text-body-md text-on-surface-variant">
        Alpaca is the source of truth for every figure here — Corollary keeps no ledger of its own.
        Showing the {ACCOUNT_LABEL[accountMode]} account.
        {account ? (
          <>
            {' '}
            Status <span className="text-data-md text-on-surface">{account.status}</span>, options
            level{' '}
            <span className="text-data-md text-on-surface">
              {account.optionsTradingLevel ?? 'not reported'}
            </span>
            .
          </>
        ) : null}
      </p>
    </>
  )

  // Not an error: the server declined to serve one book under the other's
  // name, and said why. Rendered as the page, because there is no figure on
  // it that could honestly be filled in.
  if (isAccountUnavailable(accountQuery.error)) {
    const reason =
      paperQuery.data?.cashAccountUnavailableReason ??
      (isApiError(accountQuery.error) ? accountQuery.error.message : '')
    return (
      <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
        {header}
        <CashNotConfigured
          reason={reason}
          missing={paperQuery.data?.missingLiveCredentialEnvVars ?? []}
        />
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-[1425px] px-4 py-12 lg:px-12">
      {header}

      {account && (account.accountBlocked || account.tradingBlocked) ? (
        <p role="alert" className="mt-4 max-w-prose text-body-md text-error">
          The broker reports this account as blocked
          {account.accountBlocked ? ' (account)' : ''}
          {account.tradingBlocked ? ' (trading)' : ''}. Balances below are still the broker's, but
          no order placed from here would be accepted.
        </p>
      ) : null}

      {accountQuery.isError ? (
        <div className="mt-8">
          <RequestFailed error={accountQuery.error} what="this account" />
        </div>
      ) : (
        <>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {accountQuery.isPending || !account ? (
              <>
                <StatCardSkeleton label="Loading cash" />
                <StatCardSkeleton label="Loading day change" />
                <StatCardSkeleton label="Loading buying power" />
                <StatCardSkeleton label="Loading options buying power" />
              </>
            ) : (
              <>
                <StatCard label="Cash" value={formatUsd(account.cash)} icon={<BankIcon />} />
                <StatCard
                  label="Day change"
                  // A losing day is `bearish`, never `error` — a falling
                  // balance is not a system failure. Signed textually as
                  // well, because colour alone fails in grayscale.
                  value={formatUsd(account.dayChange, { signed: true })}
                  valueClassName={signClass(account.dayChange)}
                  icon={account.dayChange < 0 ? <TrendingDownIcon /> : <TrendingUpIcon />}
                  changePct={account.balanceTrend?.changePct}
                  comparedTo={account.balanceTrend?.comparedTo}
                  // No baseline, no trend line: `lastEquity` of zero makes
                  // the percentage undefined, and 0% would claim a flat day.
                  note={account.balanceTrend ? undefined : 'No previous close to compare against'}
                />
                <StatCard
                  label="Buying power"
                  value={formatUsd(account.buyingPower)}
                  icon={<TrendingUpIcon />}
                  // Read off the broker's `multiplier` and rendered verbatim.
                  // The page must not compose this sentence: the last one it
                  // composed said "2×" on a 4× account, which overstates
                  // capacity, which is the direction that costs money.
                  note={account.margin.label}
                />
                <StatCard
                  label="Options buying power"
                  value={optionsBuyingPowerValue(account)}
                  icon={<TargetIcon />}
                  note="Options are not marginable"
                />
              </>
            )}
          </div>

          {account ? (
            <p className="mt-3 max-w-prose text-caption text-on-surface-variant">{account.margin.note}</p>
          ) : null}

          <div className="mt-4">
            <Panel
              id="equity-heading"
              title="Total equity"
              aside={
                <div className="flex items-center gap-2">
                  <span className="whitespace-nowrap text-caption text-on-surface-variant">
                    {accountQuery.dataUpdatedAt
                      ? /* What the figures actually are: a balance read at a
                           moment, not a stream. A "Live" pill over a polled
                           balance would claim more than is true. */
                        <>
                          Read{' '}
                          <span className="text-data-md">
                            {formatTimeET(new Date(accountQuery.dataUpdatedAt).toISOString())}
                          </span>{' '}
                          ET
                        </>
                      : 'Reading balances…'}
                  </span>
                  <RefreshButton
                    label="Refresh account balances"
                    // Re-reading your own balances costs nothing worth
                    // throttling; §6.5's rate limit is the recommendations
                    // rule.
                    cooldownMs={0}
                    onRefresh={() => {
                      void accountQuery.refetch()
                      void positionsQuery.refetch()
                      void transfersQuery.refetch()
                    }}
                  />
                </div>
              }
            >
              <Line
                label="Cash"
                value={
                  account ? (
                    formatUsd(account.cash)
                  ) : (
                    <ValueSkeleton label="Reading cash balance" className="w-28" />
                  )
                }
              />
              <Line
                label="Open positions"
                note={positionsQuery.data ? `${positionsQuery.data.length} held` : undefined}
                detail={
                  account ? (
                    <>
                      Long <span className="text-data-md">{formatUsd(account.longMarketValue)}</span>
                      {' · '}
                      Short{' '}
                      <span className="text-data-md">
                        {formatUsd(account.shortMarketValue, { signed: true })}
                      </span>{' '}
                      — a credit is a liability, so the short side subtracts.
                    </>
                  ) : undefined
                }
                value={
                  account ? (
                    formatUsd(account.netPositionValue)
                  ) : (
                    <ValueSkeleton label="Reading open positions" className="w-28" />
                  )
                }
              />
              <Line
                label="Total equity"
                value={
                  account ? (
                    formatUsd(account.equity)
                  ) : (
                    <ValueSkeleton label="Reading equity" className="w-32" />
                  )
                }
                total
              />

              {account ? (
                <p
                  className={`mt-4 max-w-prose text-caption ${
                    account.equityReconciles ? 'text-on-surface-variant' : 'text-caution'
                  }`}
                >
                  {account.equityReconciles ? (
                    <>
                      Alpaca reports equity of {formatUsd(account.equity)}, and cash plus the market
                      value of open positions comes to the same figure. Two sources, one number.
                    </>
                  ) : (
                    <>
                      Alpaca reports equity of {formatUsd(account.equity)}; cash plus the market
                      value of open positions comes to {formatUsd(account.derivedEquity)}, a
                      difference of {formatUsd(account.equityDifference, { signed: true })}. The
                      broker's figure is the authoritative one — the gap is shown rather than
                      hidden.
                    </>
                  )}
                </p>
              ) : null}

              <p className="mt-2 max-w-prose text-caption text-on-surface-variant">
                Cash is the full balance — some of it may not be spendable yet, and buying power
                above is what is.
                {account && account.grossPositionValue !== null ? (
                  <>
                    {' '}
                    Gross exposure, long and short added as magnitudes, is{' '}
                    <span className="text-data-md">{formatUsd(account.grossPositionValue)}</span> —
                    what is at work in the market, not a term in the equity above.
                  </>
                ) : null}
              </p>
            </Panel>
          </div>
        </>
      )}

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
          <Transfers
            isPending={transfersQuery.isPending}
            isError={transfersQuery.isError}
            error={transfersQuery.error}
            page={transfersQuery.data}
            accountMode={accountMode}
            isCashAccount={isCashAccount}
            transfersBlocked={account?.transfersBlocked ?? false}
          />
        </Panel>
      </div>
    </div>
  )
}

function Transfers({
  isPending,
  isError,
  error,
  page,
  accountMode,
  isCashAccount,
  transfersBlocked,
}: {
  isPending: boolean
  isError: boolean
  error: unknown
  page: Page<ActivityItem> | undefined
  accountMode: AccountMode
  isCashAccount: boolean
  transfersBlocked: boolean
}) {
  if (isPending) {
    return <TableSkeleton rows={3} columns={4} label="Reading cash transfers" />
  }
  if (isError || !page) {
    return <RequestFailed error={error} what="cash transfers" />
  }

  // The server already serves the non-order branch of the ledger, and this
  // filters it again through the same rule the fixtures used: there is one
  // place that knows a deposit is not a trade, and it can only ever remove a
  // row that does not belong in this table.
  const transfers = cashTransfers(page.items)
  const net = netTransfers(transfers)
  // Measured against what the server *sent*, not against what survived the
  // filter: truncation is a paging fact. Comparing the filtered count would
  // report a page holding the whole ledger as truncated the moment one row
  // was dropped.
  const truncated = page.total > page.items.length

  if (transfers.length === 0) {
    /* A designed empty state, not a blank table. "No transfers" in a book
       that has traded means something specific: the balance got here through
       P&L rather than through funding. */
    return (
      <p className="max-w-prose text-body-md text-on-surface-variant">
        No deposits or withdrawals in the {ACCOUNT_LABEL[accountMode]} account. Its balance has
        moved through trading alone. Transfers are initiated at Alpaca, not here.
      </p>
    )
  }

  return (
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
            <tr key={t.id} className="h-8 border-b border-outline-variant last:border-0">
              <td className={`${TD} whitespace-nowrap text-data-md text-on-surface-variant`}>
                {formatDateTimeET(t.time)}
              </td>
              <td className={`${TD} text-body-sm text-on-surface`}>
                {ACTIVITY_ACTION_LABEL[t.action]}
              </td>
              {/* Signed textually as well as by colour — DESIGN.md requires
                  the sign on every value, because colour alone fails in
                  grayscale and for colourblind readers. `signClass` gives
                  bullish/bearish, never `error`: a withdrawal is not a
                  failure. */}
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
          <tr className="h-8 border-t border-outline-warm">
            <td className={`${TD} text-label-md text-on-surface`} colSpan={2}>
              {/* Named for what it actually sums. A page of a longer ledger
                  nets that page, and calling it the book's net would be a
                  figure that is simply wrong. */}
              {truncated ? 'Net of the transfers shown' : 'Net transferred'}
            </td>
            <td className={`${TD} text-right text-data-md ${signClass(net)}`}>
              {formatUsd(net, { signed: true })}
            </td>
            <td className={TD} />
          </tr>
        </tfoot>
      </table>

      {truncated && (
        <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
          Showing the most recent {transfers.length} of {page.total} transfers.
        </p>
      )}

      {transfersBlocked && (
        <p className="mt-4 max-w-prose text-caption text-caution">
          The broker reports transfers as blocked on this account.
        </p>
      )}

      {!isCashAccount && (
        <p className="mt-4 max-w-prose text-caption text-on-surface-variant">
          Paper transfers are simulated by Alpaca — no money moved.
        </p>
      )}
    </>
  )
}
