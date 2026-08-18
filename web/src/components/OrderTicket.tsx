import { useEffect, useMemo, useState } from 'react'
import { ConfirmDialog } from './ConfirmDialog'
import { useUIStore } from '../lib/store'
import {
  MARKET_TODAY,
  ORDER_TYPE_LABEL,
  RISK_LIMITS,
  TIME_IN_FORCE_LABEL,
  type OrderType,
  type Position,
  type TimeInForce,
} from '../lib/mockData'
import {
  MULTI_LEG_NOTE,
  ORDER_SIDE_LABEL,
  addedRiskPct,
  daysToExpiry,
  availableOrderTypes,
  estimate,
  exitIsSell,
  isMultiLeg,
  midPrice,
  resolvedSide,
  validateExit,
  validateOrder,
  type OrderDraft,
  type TicketMode,
} from '../lib/orders'
import { formatExpiry, formatPct, formatUsd } from '../lib/format'

const MODES: { key: TicketMode; label: string }[] = [
  { key: 'close', label: 'Close' },
  { key: 'add', label: 'Add' },
  { key: 'exit', label: 'Exit' },
]

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

interface OrderTicketProps {
  position: Position
  mode: TicketMode
  onModeChange: (mode: TicketMode) => void
  /** Account equity, for the advisory risk estimate. Display only. */
  equity: number
  strategyName: string | null
}

export function OrderTicket({ position, mode, onModeChange, equity, strategyName }: OrderTicketProps) {
  const submitPositionOrder = useUIStore((s) => s.submitPositionOrder)
  const upsertExit = useUIStore((s) => s.upsertExit)
  const cancelExit = useUIStore((s) => s.cancelExit)

  const types = availableOrderTypes(position)

  const [quantity, setQuantity] = useState(String(position.quantity))
  const [orderType, setOrderType] = useState<OrderType>(types[0])
  const [limitPrice, setLimitPrice] = useState(String(midPrice(position)))
  const [stopPrice, setStopPrice] = useState('')
  const [timeInForce, setTimeInForce] = useState<TimeInForce>('day')
  const [confirming, setConfirming] = useState(false)

  const existing = position.attachedExit
  const [takeProfit, setTakeProfit] = useState(existing ? String(existing.takeProfit) : '')
  const [exitStop, setExitStop] = useState(existing ? String(existing.stopPrice) : '')
  const [exitStopLimit, setExitStopLimit] = useState(
    existing?.stopLimitPrice != null ? String(existing.stopLimitPrice) : '',
  )
  const [exitTif, setExitTif] = useState<TimeInForce>(existing?.timeInForce ?? 'gtc')

  // Switching rows reuses this component, so the draft has to follow the
  // position rather than persist across it — otherwise you inherit the
  // last position's quantity and prices without noticing.
  useEffect(() => {
    setQuantity(String(position.quantity))
    setOrderType(availableOrderTypes(position)[0])
    setLimitPrice(String(midPrice(position)))
    setStopPrice('')
    const attached = position.attachedExit
    setTakeProfit(attached ? String(attached.takeProfit) : '')
    setExitStop(attached ? String(attached.stopPrice) : '')
    setExitStopLimit(attached?.stopLimitPrice != null ? String(attached.stopLimitPrice) : '')
    setExitTif(attached?.timeInForce ?? 'gtc')
  }, [position])

  useEffect(() => {
    if (mode === 'close') setQuantity(String(position.quantity))
    if (mode === 'add') setQuantity('1')
  }, [mode, position.quantity])

  const orderMode = mode === 'exit' ? 'close' : mode
  const draft: OrderDraft = {
    mode: orderMode,
    quantity: Number(quantity) || 0,
    orderType,
    limitPrice: toNumber(limitPrice),
    stopPrice: toNumber(stopPrice),
    timeInForce,
  }

  const exitDraft = {
    takeProfit: toNumber(takeProfit),
    stopPrice: toNumber(exitStop),
    stopLimitPrice: toNumber(exitStopLimit),
    timeInForce: exitTif,
  }

  const errors = mode === 'exit' ? validateExit(position, exitDraft) : validateOrder(position, draft)
  const preview = estimate(position, draft)
  const side = resolvedSide(position, orderMode)

  const riskPct = useMemo(
    () => addedRiskPct(position, draft.quantity, equity),
    [position, draft.quantity, equity],
  )
  const riskCeiling = RISK_LIMITS.find((l) => l.key === 'max_risk_per_trade_pct')?.value ?? 0

  const dte = daysToExpiry(position.expiry, MARKET_TODAY)
  /* A GTC order cannot outlive the contract it is written on — the
     contract expires and takes the order with it. "Good til canceled" is
     the one phrase on this ticket that reads like a promise, and on a
     position seven days from expiry it is a promise about seven days. */
  const gtcOutlivesContract = (mode === 'exit' ? exitTif : timeInForce) === 'gtc'

  const needsLimit = orderType === 'limit' || orderType === 'stop_limit'
  const needsStop = orderType === 'stop' || orderType === 'stop_limit'

  function submit() {
    if (mode === 'exit') {
      if (exitDraft.takeProfit === null || exitDraft.stopPrice === null) return
      upsertExit(position.id, {
        takeProfit: exitDraft.takeProfit,
        stopPrice: exitDraft.stopPrice,
        stopLimitPrice: exitDraft.stopLimitPrice,
        timeInForce: exitDraft.timeInForce,
        // Broker-side where the broker will take it, Corollary-managed
        // where it won't. Phase 2 learns which; Phase 1 states the intent.
        heldBy: isMultiLeg(position) ? 'corollary' : 'broker',
      })
    } else {
      submitPositionOrder(position.id, draft)
    }
    setConfirming(false)
  }

  return (
    <form
      // A real form, so Enter in any field opens the confirm — the same
      // gesture every other order ticket in the world uses. It opens the
      // dialog rather than submitting outright; Enter should not be able
      // to place an order in one keystroke.
      onSubmit={(e) => {
        e.preventDefault()
        if (errors.length === 0) setConfirming(true)
      }}
    >
      <div
        role="group"
        aria-label="Ticket mode"
        className="inline-flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md"
      >
        {MODES.map((m) => (
          <button
            key={m.key}
            type="button"
            onClick={() => onModeChange(m.key)}
            aria-pressed={mode === m.key}
            className={
              mode === m.key
                ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
                : 'rounded-full px-3 py-1 text-on-surface-variant hover:text-on-surface'
            }
          >
            {m.key === 'exit' ? (existing ? 'Edit exit' : 'Attach exit') : m.label}
          </button>
        ))}
      </div>

      {mode === 'exit' ? (
        <div className="mt-4 space-y-3">
          <p className="text-body-md text-on-surface-variant">
            {exitIsSell(position)
              ? 'Sells to close at the take-profit, or at the stop if it trips first.'
              : 'Buys back at the take-profit, or at the stop if it trips first.'}{' '}
            Only one of the two can fill.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Take profit">
              <input
                type="number"
                step="0.01"
                inputMode="decimal"
                value={takeProfit}
                onChange={(e) => setTakeProfit(e.target.value)}
                className={FIELD}
              />
            </Field>
            <Field label="Stop">
              <input
                type="number"
                step="0.01"
                inputMode="decimal"
                value={exitStop}
                onChange={(e) => setExitStop(e.target.value)}
                className={FIELD}
              />
            </Field>
            <Field label="Stop limit (optional)">
              <input
                type="number"
                step="0.01"
                inputMode="decimal"
                value={exitStopLimit}
                onChange={(e) => setExitStopLimit(e.target.value)}
                className={FIELD}
              />
            </Field>
            <Field label="Time in force">
              <select
                value={exitTif}
                onChange={(e) => setExitTif(e.target.value as TimeInForce)}
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
          <p className="text-caption text-on-surface-variant">
            Leaving stop limit empty places a plain stop, which fills at market once it trips.
          </p>
          {gtcOutlivesContract && (
            /* `caution`, not `error`: this is a fact about the contract,
               not a mistake in the order. */
            <p className="text-caption text-caution">
              Good-til-canceled, but {position.symbol} {position.contract} expires{' '}
              {formatExpiry(position.expiry)}
              {dte > 0 ? ` — ${dte} day${dte === 1 ? '' : 's'} away` : dte === 0 ? ' — today' : ' — already expired'}.
              The order dies with the contract; it cannot outlive it.
            </p>
          )}
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Quantity">
              <input
                type="number"
                min="1"
                step="1"
                inputMode="numeric"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                className={FIELD}
              />
            </Field>
            <Field label="Order type">
              <select
                value={orderType}
                onChange={(e) => setOrderType(e.target.value as OrderType)}
                className={FIELD}
              >
                {types.map((t) => (
                  <option key={t} value={t}>
                    {ORDER_TYPE_LABEL[t]}
                  </option>
                ))}
              </select>
            </Field>
            {needsLimit && (
              <Field label="Limit price">
                <input
                  type="number"
                  step="0.01"
                  inputMode="decimal"
                  value={limitPrice}
                  onChange={(e) => setLimitPrice(e.target.value)}
                  className={FIELD}
                />
              </Field>
            )}
            {needsStop && (
              <Field label="Stop price">
                <input
                  type="number"
                  step="0.01"
                  inputMode="decimal"
                  value={stopPrice}
                  onChange={(e) => setStopPrice(e.target.value)}
                  className={FIELD}
                />
              </Field>
            )}
            <Field label="Time in force">
              <select
                value={timeInForce}
                onChange={(e) => setTimeInForce(e.target.value as TimeInForce)}
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

          {isMultiLeg(position) && <p className="text-caption text-on-surface-variant">{MULTI_LEG_NOTE}</p>}

          {gtcOutlivesContract && (
            /* `caution`, not `error`: this is a fact about the contract,
               not a mistake in the order. */
            <p className="text-caption text-caution">
              Good-til-canceled, but {position.symbol} {position.contract} expires{' '}
              {formatExpiry(position.expiry)}
              {dte > 0 ? ` — ${dte} day${dte === 1 ? '' : 's'} away` : dte === 0 ? ' — today' : ' — already expired'}.
              The order dies with the contract; it cannot outlive it.
            </p>
          )}

          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 border-t border-outline/10 pt-3 text-label-md">
            <dt className="text-on-surface-variant">Side</dt>
            <dd className="text-right text-on-surface">{ORDER_SIDE_LABEL[side]}</dd>
            <dt className="text-on-surface-variant">Bid / ask</dt>
            <dd className="text-right text-data-md text-on-surface">
              {formatUsd(position.bid)} / {formatUsd(position.ask)}
            </dd>
            <dt className="text-on-surface-variant">
              Estimated {preview.kind === 'proceeds' ? 'proceeds' : 'cost'}
            </dt>
            <dd className="text-right text-data-md text-on-surface">{formatUsd(preview.amount)}</dd>
          </dl>

          {mode === 'add' && (
            /* Advisory, never an approval. CLAUDE.md rule 4: the engine
               enforces limits and a number from the client is not trusted.
               This exists so a rejection is unsurprising, not to pre-empt
               one — the ticket still submits and lets the risk manager
               decide. */
            <p className="text-caption text-on-surface-variant">
              Adds an estimated {formatPct(riskPct)} of equity against a {riskCeiling}% per-trade ceiling.
              The risk manager decides; this is an estimate.
            </p>
          )}
        </div>
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

      <div className="mt-4 flex items-center gap-2">
        <button
          type="submit"
          disabled={errors.length > 0}
          className="rounded bg-primary px-4 py-2 text-label-md text-on-primary transition-colors duration-base ease-standard hover:bg-primary-container hover:text-on-primary-container disabled:pointer-events-none disabled:bg-surface-container-high disabled:text-on-surface-variant"
        >
          {mode === 'exit' ? (existing ? 'Update exit' : 'Attach exit') : `Review ${MODES.find((m) => m.key === mode)!.label.toLowerCase()}`}
        </button>
        {mode === 'exit' && existing && (
          <button
            type="button"
            onClick={() => cancelExit(position.id)}
            className="rounded border border-error px-4 py-2 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container"
          >
            Cancel exit
          </button>
        )}
      </div>

      <ConfirmDialog
        open={confirming}
        title={mode === 'exit' ? 'Attach this exit?' : mode === 'add' ? 'Add to this position?' : 'Close this position?'}
        destructive={mode === 'close'}
        confirmLabel={mode === 'exit' ? 'Attach exit' : mode === 'add' ? 'Add to position' : 'Close position'}
        consequence={
          mode === 'exit' ? (
            <>
              Attaches a take-profit at {formatUsd(exitDraft.takeProfit ?? 0)} and a stop at{' '}
              {formatUsd(exitDraft.stopPrice ?? 0)}
              {exitDraft.stopLimitPrice !== null && <> (limit {formatUsd(exitDraft.stopLimitPrice)})</>} on{' '}
              {position.symbol} {position.contract}, good {TIME_IN_FORCE_LABEL[exitTif].toLowerCase()}. Only
              one of the two can fill.
              {strategyName && (
                <>
                  {' '}
                  This takes the position off {strategyName}, whose profit target and stop no longer apply.
                </>
              )}
            </>
          ) : (
            <>
              {ORDER_SIDE_LABEL[side]} — {draft.quantity} × {position.symbol} {position.contract} as a{' '}
              {ORDER_TYPE_LABEL[orderType].toLowerCase()} order
              {needsLimit && <> at {formatUsd(draft.limitPrice ?? 0)}</>}
              {needsStop && <>, stop {formatUsd(draft.stopPrice ?? 0)}</>}, good{' '}
              {TIME_IN_FORCE_LABEL[timeInForce].toLowerCase()} on a contract expiring{' '}
              {formatExpiry(position.expiry)}. Estimated{' '}
              {preview.kind === 'proceeds' ? 'proceeds' : 'cost'} {formatUsd(preview.amount)} at{' '}
              {formatUsd(preview.pricePerContract)} per contract.{' '}
              {mode === 'close'
                ? draft.quantity >= position.quantity
                  ? 'This closes the position entirely and cancels any exit attached to it.'
                  : `${position.quantity - draft.quantity} contract${position.quantity - draft.quantity === 1 ? '' : 's'} remain open, and any attached exit is cancelled.`
                : `The position becomes ${position.quantity + draft.quantity} contracts.`}
            </>
          )
        }
        onConfirm={submit}
        onCancel={() => setConfirming(false)}
      />
    </form>
  )
}
