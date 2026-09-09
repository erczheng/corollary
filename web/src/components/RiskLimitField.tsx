import { useState } from 'react'
import { ConfirmDialog } from './ConfirmDialog'
import { isRaise, riskDollars, validateRiskLimit } from '../lib/settings'
import { formatUsd } from '../lib/format'
import type { RiskLimit } from '../lib/mockData'

/** One editable risk ceiling.
 *
 * Three decisions worth stating, because each one is about not letting a
 * ceiling move by accident:
 *
 * **Nothing commits on blur.** The value is held locally and a Save button
 * appears only once it differs and validates. Tabbing out of a field is not a
 * decision, and on a control that governs how much money one trade may lose,
 * "I clicked away and it saved" is not an acceptable failure mode.
 *
 * **Only a raise confirms.** Lowering a ceiling reduces what is at risk and
 * asking twice for it teaches the habit of dismissing the dialog — which is
 * the habit that matters when the dialog is about a raise.
 *
 * **The confirm quotes dollars.** "7% → 10%" is not a consequence anyone can
 * feel. "$1,443 → $2,062 at risk on one position" is. DESIGN.md requires a
 * consequential confirm to state its consequence concretely.
 */
export function RiskLimitField({
  limit,
  equity,
  onCommit,
}: {
  limit: RiskLimit
  /** Account equity, used only to express a percentage ceiling in money. */
  equity: number
  onCommit: (value: number) => void
}) {
  const [raw, setRaw] = useState(String(limit.value))
  const [confirming, setConfirming] = useState(false)

  const parsed = raw.trim() === '' ? Number.NaN : Number(raw)
  const error = validateRiskLimit(limit, parsed)
  const dirty = raw.trim() !== '' && parsed !== limit.value
  const canSave = dirty && error === null

  const fieldId = `risk-${limit.key}`
  const errorId = `${fieldId}-error`
  const helpId = `${fieldId}-help`

  function save() {
    if (!canSave) return
    // A raise loosens the ceiling. That is the direction worth stopping on.
    if (isRaise(limit, parsed)) {
      setConfirming(true)
      return
    }
    onCommit(parsed)
  }

  return (
    <div className="border-b border-outline-variant py-4 last:border-0">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <label htmlFor={fieldId} className="text-body-md text-on-surface">
          {limit.label}
        </label>

        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            <input
              id={fieldId}
              type="number"
              inputMode="decimal"
              value={raw}
              min={limit.min}
              max={limit.max}
              step={limit.unit === 'count' ? 1 : 'any'}
              aria-describedby={error ? errorId : helpId}
              aria-invalid={error !== null}
              onChange={(e) => setRaw(e.target.value)}
              className={`w-24 rounded border bg-surface px-2 py-1 text-right text-data-md text-on-surface focus:border-primary ${
                error ? 'border-error' : 'border-outline'
              }`}
            />
            {limit.unit === '%' ? (
              <span className="text-body-md text-on-surface-variant">%</span>
            ) : null}
          </div>

          {/* Present only when there is something to save, so the row is a
              readout at rest rather than a form permanently asking to be
              submitted. */}
          {canSave ? (
            <button
              type="button"
              onClick={save}
              className="rounded bg-primary px-3 py-1 text-label-md text-on-primary"
            >
              Save
            </button>
          ) : null}
        </div>
      </div>

      <p id={helpId} className="mt-1 max-w-prose text-caption text-on-surface-variant">
        {limit.help} Range {limit.min}–{limit.max}
        {limit.unit === '%' ? '%' : ''}.
      </p>

      {error ? (
        <p id={errorId} role="alert" className="mt-1 text-caption text-error">
          {error}
        </p>
      ) : null}

      <ConfirmDialog
        open={confirming}
        title={`Raise ${limit.label.toLowerCase()}?`}
        consequence={
          limit.unit === '%' ? (
            <>
              Raising this from {limit.value}% to {parsed}% takes the ceiling on one position from{' '}
              <span className="text-data-md text-on-surface">{formatUsd(riskDollars(equity, limit.value))}</span>{' '}
              to{' '}
              <span className="text-data-md text-on-surface">{formatUsd(riskDollars(equity, parsed))}</span>{' '}
              against current equity. The engine will permit larger losses from now on.
            </>
          ) : (
            <>
              Raising this from {limit.value} to {parsed} lets the engine hold {parsed} positions at
              once instead of {limit.value}. More positions open means more exposure and less cash
              in reserve.
            </>
          )
        }
        confirmLabel="Raise limit"
        onConfirm={() => {
          onCommit(parsed)
          setConfirming(false)
        }}
        onCancel={() => setConfirming(false)}
      />
    </div>
  )
}
