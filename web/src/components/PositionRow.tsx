import { useEffect, useRef, useState } from 'react'
import { PositionChart } from './PositionChart'
import { OrderTicket } from './OrderTicket'
import { ChevronDownIcon } from './icons'
import { useUIStore } from '../lib/store'
import { formatStrategyName, formatPct, formatUsd, signClass } from '../lib/format'
import { MARKET_TODAY, TIME_IN_FORCE_LABEL, type Position } from '../lib/mockData'
import { daysToExpiry, expiryUrgency, isMultiLeg, type TicketMode } from '../lib/orders'
import { formatExpiry } from '../lib/format'

const CELL = 'px-3 py-1 text-right text-data-md text-on-surface'

/** Who is responsible for closing this position, in one line.
 *
 * The distinction the last clause draws is not cosmetic: a broker-held
 * exit fires whether or not Corollary is running, and a Corollary-held one
 * does not exist while the engine is down. */
function ExitStatus({ position, strategyName }: { position: Position; strategyName: string | null }) {
  const { managedExit, attachedExit } = position

  if (managedExit && strategyName) {
    return (
      <p className="text-label-md text-on-surface-variant">
        Managed by <span className="text-on-surface">{strategyName}</span> — target {managedExit.profitTargetPct}%,
        stop {managedExit.stopLossPct}%, {managedExit.timeStopDte} DTE
      </p>
    )
  }

  if (attachedExit) {
    return (
      <p className="text-label-md text-on-surface-variant">
        Manually managed — take-profit{' '}
        <span className="text-data-md text-on-surface">{formatUsd(attachedExit.takeProfit)}</span>, stop{' '}
        <span className="text-data-md text-on-surface">{formatUsd(attachedExit.stopPrice)}</span>
        {attachedExit.stopLimitPrice !== null && (
          <> (limit <span className="text-data-md text-on-surface">{formatUsd(attachedExit.stopLimitPrice)}</span>)</>
        )}
        , {TIME_IN_FORCE_LABEL[attachedExit.timeInForce]} · exit held{' '}
        {attachedExit.heldBy === 'broker' ? 'at broker' : 'by Corollary'}
      </p>
    )
  }

  return (
    <p className="text-label-md text-caution">
      No exit on this position — neither a strategy nor a manual order will close it.
    </p>
  )
}

function ActionsMenu({
  position,
  openedByName,
  onSelect,
  onDetach,
  onReattach,
}: {
  position: Position
  openedByName: string | null
  onSelect: (mode: TicketMode) => void
  onDetach: () => void
  onReattach: () => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  // Focus moves into the menu on open and back to the button on close.
  // Without the second half, dismissing the menu drops focus onto <body>
  // and the next Tab restarts from the top of the document — which on a
  // page of four expandable rows means finding your place again.
  useEffect(() => {
    if (open) ref.current?.querySelector('button')?.focus()
    else triggerRef.current?.focus()
  }, [open])

  const item =
    'block w-full px-3 py-2 text-left text-label-md text-on-surface hover:bg-surface-container-low'

  return (
    <div className="relative inline-block" ref={ref}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`More actions for ${position.symbol} ${position.contract}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="rounded border border-outline px-2 py-1 text-label-md text-on-surface-variant transition-colors duration-base ease-standard hover:bg-surface-container-low"
      >
        ⋯
      </button>
      {open && (
        <>
          {/* Click-anywhere-else closes. A menu you can only dismiss by
              picking something from it is a trap. */}
          <div className="fixed inset-0 z-10" role="presentation" onClick={() => setOpen(false)} />
          <div role="menu" className="absolute right-0 z-20 mt-1 w-56 overflow-hidden rounded-lg border border-outline-warm bg-surface-container-lowest py-1 text-left">
            <button type="button" className={item} onClick={() => { onSelect('add'); setOpen(false) }}>
              Add to position
            </button>
            <button type="button" className={item} onClick={() => { onSelect('exit'); setOpen(false) }}>
              {position.attachedExit ? 'Edit exit' : 'Attach exit'}
            </button>
            {position.strategyId ? (
              <button type="button" className={item} onClick={() => { onDetach(); setOpen(false) }}>
                Detach from strategy
              </button>
            ) : (
              <button type="button" className={item} onClick={() => { onReattach(); setOpen(false) }}>
                {/* Names the destination. "Reattach to strategy" reads as
                    though it could mean the active one. */}
                {openedByName ? `Reattach to ${openedByName}` : 'Reattach to strategy'}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  )
}

interface PositionRowProps {
  position: Position
  expanded: boolean
  mode: TicketMode
  onToggle: () => void
  onSelectMode: (mode: TicketMode) => void
  equity: number
  columnCount: number
}

export function PositionRow({
  position,
  expanded,
  mode,
  onToggle,
  onSelectMode,
  equity,
  columnCount,
}: PositionRowProps) {
  // Escape collapses the panel. Expanding a row is a mode you can end up
  // in by accident, and every other dismissible surface here — the
  // confirm dialog, the command palette, the ⋯ menu — already answers to
  // Escape.
  useEffect(() => {
    if (!expanded) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onToggle()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expanded, onToggle])

  const detachFromStrategy = useUIStore((s) => s.detachFromStrategy)
  const reattachToStrategy = useUIStore((s) => s.reattachToStrategy)

  /* From the store, not the fixture: Research can rename a strategy, and a
     row reading the fixture would keep showing the old name while Research
     showed the new one — two names for one strategy on one app. */
  const strategies = useUIStore((s) => s.strategies)

  const strategy = strategies.find((s) => s.id === position.strategyId)
  const strategyName = strategy ? formatStrategyName(strategy.name) : null
  // Names the strategy that *opened* it, which is where reattaching sends
  // it back to — not whichever strategy is active now.
  const openedBy = strategies.find((s) => s.id === position.openedByStrategyId)
  const openedByName = openedBy ? formatStrategyName(openedBy.name) : null
  const underlying = useUIStore((s) => s.underlyings[position.symbol]) ?? null
  const dte = daysToExpiry(position.expiry, MARKET_TODAY)
  const urgency = expiryUrgency(position, MARKET_TODAY)

  return (
    <>
      <tr className="border-t border-outline/10 hover:bg-surface-container-low">
        <td className="max-w-0 px-3 py-1 text-body-md text-on-surface">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            // Named explicitly rather than taking the row's text as its
            // accessible name: "AAPL $230 Call Oct 17" doesn't tell anyone
            // the control expands anything.
            aria-label={`Details for ${position.symbol} ${position.contract}`}
            className="flex w-full items-center gap-2 text-left"
          >
            <ChevronDownIcon
              className={`h-3.5 w-3.5 shrink-0 text-on-surface-variant transition-transform duration-base ease-standard ${
                expanded ? 'rotate-180' : ''
              }`}
            />
            <span className="block truncate" title={`${position.symbol} ${position.contract}`}>
              <span className="text-data-md">{position.symbol}</span> {position.contract}
            </span>
            {isMultiLeg(position) && (
              <span className="shrink-0 rounded-full bg-neutral-container px-2 py-0.5 text-caption text-on-neutral-container">
                {position.legs.length} legs
              </span>
            )}
          </button>
        </td>
        {/* Days to expiry. The most time-sensitive fact about an option,
            and the page said nothing about it until now — it lived only
            inside the contract string, where nothing could count it down.
            `caution`, never `error`: a position running out of time is
            not a system failure, it's a deadline. */}
        <td className={`${CELL} ${urgency === 'normal' ? '' : 'text-caution'}`}>
          <span title={`Expires ${formatExpiry(position.expiry)}`}>
            {dte < 0 ? 'Expired' : dte === 0 ? 'Today' : `${dte}d`}
          </span>
        </td>
        <td className={CELL}>{formatUsd(position.last)}</td>
        <td className={CELL}>{formatUsd(position.costBasis)}</td>
        <td className={CELL}>{formatUsd(position.value)}</td>
        <td className={CELL}>{position.quantity}</td>
        <td className={`whitespace-nowrap px-3 py-1 text-right text-data-md ${signClass(position.pnl)}`}>
          {formatUsd(position.pnl, { signed: true })}
          <span className="ml-2 text-caption">{formatPct(position.pnlPct, { signed: true })}</span>
        </td>
        <td className="whitespace-nowrap px-3 py-1 text-right">
          <div className="flex items-center justify-end gap-2">
            {/* Close is the order button: it opens the ticket rather than
                submitting. One word, one behaviour — the old fast-and-
                irreversible Close and a slow deliberate one would have been
                the same word doing two different things. */}
            <button
              type="button"
              onClick={() => onSelectMode('close')}
              title={`Close ${position.symbol} ${position.contract}`}
              className="rounded border border-error px-3 py-1 text-label-md text-error transition-colors duration-base ease-standard hover:bg-error-container"
            >
              Close
            </button>
            <ActionsMenu
              position={position}
              openedByName={openedByName}
              onSelect={onSelectMode}
              onDetach={() => detachFromStrategy(position.id)}
              onReattach={() => reattachToStrategy(position.id)}
            />
          </div>
        </td>
      </tr>

      {expanded && (
        <tr className="border-t border-outline/10 bg-surface-container-lowest">
          <td colSpan={columnCount} className="px-3 py-4">
            <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
              <ExitStatus position={position} strategyName={strategyName} />
              {/* The underlying, stated, with its day. The Last column
                  shows the *contract's* mark, which is what you close at,
                  so without this the price the payoff curve is drawn
                  against appears nowhere as a number — and the stock's own
                  day, which is what actually moved your position, appears
                  nowhere at all. */}
              <p className="whitespace-nowrap text-label-md text-on-surface-variant">
                {position.symbol} underlying{' '}
                <span className="text-data-md text-on-surface">
                  {formatUsd(underlying?.price ?? position.underlying)}
                </span>
                {underlying && (
                  <span className={`ml-2 text-data-md ${signClass(underlying.change)}`}>
                    {formatUsd(underlying.change, { signed: true })}{' '}
                    <span className="text-caption">
                      {formatPct(underlying.changePct, { signed: true })} today
                    </span>
                  </span>
                )}
              </p>
            </div>
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,26rem)]">
              <PositionChart position={position} />
              <OrderTicket
                position={position}
                mode={mode}
                onModeChange={onSelectMode}
                equity={equity}
                strategyName={strategyName}
              />
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
