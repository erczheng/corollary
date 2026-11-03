import type { ReactNode } from 'react'

interface ConfirmDialogProps {
  open: boolean
  title: string
  /** State the consequence concretely — never "Are you sure?" (DESIGN.md,
   * Interface voice). */
  consequence: ReactNode
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  /** Destructive confirms use the error-outlined button; the Cash switch
   * uses the ordinary primary button since it isn't destructive, just
   * consequential. */
  destructive?: boolean
  /** Holds the confirm shut — while the facts it names are still loading or
   * could not be read, or while the action is already in flight. */
  confirmDisabled?: boolean
  /** Holds Cancel (and the backdrop) shut while an action is in flight, so the
   * dialog cannot close over a request whose answer still changes state. */
  cancelDisabled?: boolean
  /** Status below the consequence: a refusal, a loading line. */
  children?: ReactNode
}

export function ConfirmDialog({
  open,
  title,
  consequence,
  confirmLabel,
  onConfirm,
  onCancel,
  destructive = false,
  confirmDisabled = false,
  cancelDisabled = false,
  children,
}: ConfirmDialogProps) {
  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-inverse-surface/40 p-6"
      role="presentation"
      onClick={cancelDisabled ? undefined : onCancel}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="w-full max-w-md rounded-xl border border-outline-warm bg-surface-container-lowest p-6 text-on-surface"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="confirm-dialog-title" className="text-title-lg">
          {title}
        </h2>
        <p className="mt-3 text-body-md text-on-surface-variant">
          {consequence}
        </p>
        {children}
        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelDisabled}
            className="rounded border border-outline px-4 py-2 text-label-md text-on-surface disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={confirmDisabled}
            className={
              (destructive
                ? 'rounded border border-error px-4 py-2 text-label-md text-error'
                : 'rounded bg-primary px-4 py-2 text-label-md text-on-primary') +
              ' disabled:cursor-not-allowed disabled:opacity-50'
            }
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
