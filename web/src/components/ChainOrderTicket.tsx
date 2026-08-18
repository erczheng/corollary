import { useEffect, useState } from 'react'
import { ConfirmDialog } from './ConfirmDialog'
import { useUIStore } from '../lib/store'
import { contractLabel } from '../lib/store'
import {
  ORDER_TYPE_LABEL,
  RISK_LIMITS,
  TIME_IN_FORCE_LABEL,
  type OptionContract,
  type OrderType,
  type TimeInForce,
} from '../lib/mockData'
import {
  OPEN_SIDES,
  ORDER_SIDE_LABEL,
  estimateOpen,
  midPrice,
  openCrossingPrice,
  openRisk,
  validateOpenOrder,
  type OpenDraft,
  type OpenSide,
} from '../lib/orders'
import { contractMoneyness } from '../lib/markets'
import { formatIv, formatPct, formatUsd } from '../lib/format'

/** Opening orders accept market and limit only.
 *
 * Not a technical limit — a stop *to open* an option is a rare thing and a
 * reliable way to buy a contract into the move that triggered it, at
 * whatever the spread has widened to by then. Closing keeps its stops,
 * where protecting a position you already hold is the whole point. */
export const OPEN_ORDER_TYPES: OrderType[] = ['market', 'limit']

const FIELD =
  'w-full rounded border border-outline bg-surface px-2 py-2 text-data-md text-on-surface focus:border-primary'
const LABEL = 'block text-label-md text-on-surface-variant'

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className={LABEL}>{label}</span>
      <span className="mt-1 block">{children}</span>
    </label>
  )
}

/** Empty is null, not zero — a blank price field means "not stated yet",
 * and treating it as 0 would make validation pass with a free order. */
function toNumber(raw: string): number | null {
  if (raw.trim() === '') return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

const MAX_RISK_PCT = RISK_LIMITS.find((l) => l.key === 'max_risk_per_trade_pct')?.value ?? 7

interface ChainOrderTicketProps {
  contract: OptionContract
  /** Account equity, for the advisory risk estimate. Display only — the
   * engine is what enforces the ceiling (CLAUDE.md rule 4). */
  equity: number
  onDone: () => void
}

/** Opens a position from a chain row.
 *
 * Deliberately not `OrderTicket`. That one acts on something you hold: it
 * knows the quantity you own, whether closing means selling or buying back,
 * and which exit is attached. None of that exists yet here, and the one
 * thing this ticket has that it doesn't is a *choice of side* — buy to open
 * or sell to open, which on the chain is the user's decision rather than a
 * consequence of a direction already taken.
 *
 * What the two do share is `orders.ts`, so the bid/ask rules are written
 * once. Selling hits the bid and buying lifts the ask in both.
 */
export function ChainOrderTicket({ contract, equity, onDone }: ChainOrderTicketProps) {
  const submitOpenOrder = useUIStore((s) => s.submitOpenOrder)
  const isHalted = useUIStore((s) => s.isHalted)
  // From the store, not the fixture: the poll moves it, and a frozen spot
  // beside a moving chain would put the sentence and the row in
  // disagreement about the same stock.
  const spot = useUIStore((s) => s.underlyings[contract.symbol])?.price ?? null

  const [side, setSide] = useState<OpenSide>('BTO')
  const [quantity, setQuantity] = useState('1')
  const [orderType, setOrderType] = useState<OrderType>('limit')
  const [limitPrice, setLimitPrice] = useState(String(midPrice(contract)))
  const [timeInForce, setTimeInForce] = useState<TimeInForce>('day')
  const [confirming, setConfirming] = useState(false)

  // Expanding a different row reuses this component, so the draft follows
  // the contract rather than persisting across it — otherwise you inherit
  // a limit price written for a strike you are no longer looking at.
  const key = `${contract.symbol}-${contract.expiration}-${contract.strike}-${contract.type}`
  useEffect(() => {
    setSide('BTO')
    setQuantity('1')
    setOrderType('limit')
    setTimeInForce('day')
    setConfirming(false)
  }, [key])

  // The mid moves with the poll. Reseeding the limit on every print would
  // overwrite what you typed, so it only follows the contract.
  useEffect(() => {
    setLimitPrice(String(midPrice(contract)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const draft: OpenDraft = {
    side,
    quantity: Number(quantity),
    orderType,
    limitPrice: toNumber(limitPrice),
    stopPrice: null,
    timeInForce,
  }

  const errors = validateOpenOrder(draft)
  const est = estimateOpen(contract, draft)
  const risk = openRisk(contract, draft, equity)
  const name = `${contract.symbol} ${contractLabel(contract)}`
  const crossing = openCrossingPrice(contract, side)

  const overLimit = risk.kind === 'defined' && risk.pct > MAX_RISK_PCT
  const money = contractMoneyness(spot ?? contract.strike, contract.strike, contract.type)

  function submit() {
    submitOpenOrder(contract, draft)
    onDone()
  }

  return (
    <div className="border-t border-outline-warm bg-surface-container-low px-4 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-title-lg text-on-surface">{name}</h3>
        <p className="text-caption text-on-surface-variant">
          Bid {formatUsd(contract.bid)} · Ask {formatUsd(contract.ask)} · IV {formatIv(contract.iv)}
        </p>
      </div>

      {/* One sentence, not a chart. Where the stock sits against the
          strike is the fact that decides what this contract is worth at
          expiry, and the ticket otherwise shows only bid, ask and IV — so
          the underlying would not be on screen anywhere near the order you
          are about to place. The chart itself lives in the stock table,
          which is where you browse rather than transact. */}
      {spot !== null && (
        <p className="mt-3 text-caption text-on-surface-variant">
          {contract.symbol} at{' '}
          <span className="text-data-md text-on-surface">{formatUsd(spot)}</span> is{' '}
          <span className={money.itm ? 'text-on-surface' : undefined}>
            {money.itm ? 'in the money' : 'out of the money'}
          </span>{' '}
          against the {formatUsd(contract.strike)} {contract.type}, by {formatUsd(money.distance)}.
        </p>
      )}

      {/* Side first, because it changes what every field below means — the
          estimate flips from a cost to a credit and the risk stops being a
          number this page can state. */}
      <div className="mt-4 flex gap-2" role="group" aria-label="Order side">
        {OPEN_SIDES.map((s) => (
          <button
            key={s}
            type="button"
            aria-pressed={side === s}
            onClick={() => setSide(s)}
            className={`rounded px-3 py-2 text-label-md transition-colors duration-base ease-standard ${
              side === s
                ? 'bg-primary-container text-on-primary-container'
                : 'border border-outline text-on-surface-variant hover:bg-surface-container'
            }`}
          >
            {ORDER_SIDE_LABEL[s]}
          </button>
        ))}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Field label="Contracts">
          <input
            type="number"
            min={1}
            step={1}
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            aria-label="Contracts"
            className={FIELD}
          />
        </Field>
        <Field label="Order type">
          <select
            value={orderType}
            onChange={(e) => setOrderType(e.target.value as OrderType)}
            aria-label="Order type"
            className={FIELD}
          >
            {OPEN_ORDER_TYPES.map((t) => (
              <option key={t} value={t}>
                {ORDER_TYPE_LABEL[t]}
              </option>
            ))}
          </select>
        </Field>
        {orderType === 'limit' && (
          <Field label="Limit price">
            <input
              type="number"
              min={0.01}
              step={0.01}
              value={limitPrice}
              onChange={(e) => setLimitPrice(e.target.value)}
              aria-label="Limit price"
              className={FIELD}
            />
          </Field>
        )}
        <Field label="Time in force">
          <select
            value={timeInForce}
            onChange={(e) => setTimeInForce(e.target.value as TimeInForce)}
            aria-label="Time in force"
            className={FIELD}
          >
            {(['day', 'gtc'] as TimeInForce[]).map((t) => (
              <option key={t} value={t}>
                {TIME_IN_FORCE_LABEL[t]}
              </option>
            ))}
          </select>
        </Field>
      </div>

      <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2 text-label-md">
        <div>
          <dt className="text-on-surface-variant">
            {est.kind === 'cost' ? 'Estimated cost' : 'Estimated credit'}
          </dt>
          <dd className="text-data-md text-on-surface">{formatUsd(est.amount)}</dd>
        </div>
        <div>
          <dt className="text-on-surface-variant">
            {orderType === 'market' ? (side === 'BTO' ? 'Lifts the ask' : 'Hits the bid') : 'At limit'}
          </dt>
          <dd className="text-data-md text-on-surface">
            {formatUsd(orderType === 'market' ? crossing : (draft.limitPrice ?? crossing))}
          </dd>
        </div>
        <div>
          <dt className="text-on-surface-variant">Risk</dt>
          <dd
            className={`text-data-md ${
              risk.kind === 'undefined'
                ? 'text-caution'
                : overLimit
                  ? 'text-error'
                  : 'text-on-surface'
            }`}
          >
            {risk.kind === 'undefined' ? (
              'Undefined'
            ) : (
              <>
                {formatUsd(risk.amount)}{' '}
                <span className="text-label-md">{formatPct(risk.pct)} of equity</span>
              </>
            )}
          </dd>
        </div>
      </dl>

      {/* A naked short has no maximum loss to quote, so the ticket says so
          rather than printing a confident wrong number under the word
          "risk". The engine sizes it against a ±2σ stress loss on the
          underlying's 20-day realized vol (CLAUDE.md rule 4) — data this
          page does not have and should not guess at. */}
      {risk.kind === 'undefined' && (
        <p className="mt-2 max-w-prose text-caption text-caution">
          Selling to open is an undefined-risk position. The engine sizes it against a ±2σ stress
          loss on {contract.symbol}’s 20-day realized volatility, which is computed server-side —
          this ticket cannot state a maximum loss, because there isn’t one.
        </p>
      )}

      {overLimit && (
        <p className="mt-2 max-w-prose text-caption text-error">
          {formatPct(risk.kind === 'defined' ? risk.pct : 0)} of equity is over the{' '}
          {MAX_RISK_PCT}% per-trade ceiling. The engine enforces this limit and will reject the
          order; this warning is advisory only.
        </p>
      )}

      {errors.length > 0 && (
        <ul className="mt-3 space-y-1">
          {errors.map((e) => (
            <li key={e} className="text-caption text-error">
              {e}
            </li>
          ))}
        </ul>
      )}

      {isHalted && (
        <p className="mt-3 text-caption text-caution">
          Trading is halted. Halt stops new entries — resume from the Dashboard before opening a
          position.
        </p>
      )}

      <div className="mt-4 flex gap-2">
        <button
          type="button"
          disabled={errors.length > 0 || isHalted}
          onClick={() => setConfirming(true)}
          className="rounded bg-primary px-4 py-2 text-label-md text-on-primary transition-colors duration-base ease-standard hover:bg-primary-container hover:text-on-primary-container disabled:pointer-events-none disabled:bg-surface-container-high disabled:text-on-surface-variant"
        >
          {/* Not "Buy to open" — that is the name of the side *selector*
              beside it, and two buttons under one name in one ticket is
              ambiguous to anyone not looking at the highlight. "Review" is
              also what actually happens: the confirm comes next. */}
          Review {side === 'BTO' ? 'buy to open' : 'sell to open'}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="rounded border border-outline px-4 py-2 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container"
        >
          Cancel
        </button>
      </div>

      <ConfirmDialog
          open={confirming}
          title={`${ORDER_SIDE_LABEL[side]}?`}
          // Concrete terms, not "are you sure": the contract, the size, the
          // money, and what happens if it doesn't fill straight away.
          consequence={
            <>
              <p>
                {side === 'BTO' ? 'Buying' : 'Selling'} {draft.quantity} contract
                {draft.quantity === 1 ? '' : 's'} of <strong>{name}</strong>
                {orderType === 'market'
                  ? ` at market, ${side === 'BTO' ? 'lifting the ask' : 'hitting the bid'} near ${formatUsd(crossing)}.`
                  : ` at a ${formatUsd(draft.limitPrice ?? 0)} limit, ${TIME_IN_FORCE_LABEL[timeInForce].toLowerCase()}.`}
              </p>
              <p className="mt-2">
                {est.kind === 'cost' ? 'Estimated cost' : 'Estimated credit'} {formatUsd(est.amount)}
                {risk.kind === 'defined'
                  ? `, risking ${formatUsd(risk.amount)} — ${formatPct(risk.pct)} of this account.`
                  : '. Maximum loss is undefined on a short.'}
              </p>
              {orderType === 'limit' && (
                <p className="mt-2">
                  A limit order rests until the contract reaches it. It appears under Working Orders
                  on Activity until then.
                </p>
              )}
            </>
          }
          confirmLabel={ORDER_SIDE_LABEL[side]}
          onConfirm={() => {
            setConfirming(false)
            submit()
          }}
          onCancel={() => setConfirming(false)}
        />
    </div>
  )
}
