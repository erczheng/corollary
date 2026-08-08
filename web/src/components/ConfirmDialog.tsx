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
}

export function ConfirmDialog({
  open,
  title,
  consequence,
  confirmLabel,
  onConfirm,
  onCancel,
  destructive = false,
}: ConfirmDialogProps) {
  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-inverse-surface/40 p-6"
      role="presentation"
      onClick={onCancel}
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
        <div className="mt-6 flex justify-end gap-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-outline px-4 py-2 text-label-md text-on-surface"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={
              destructive
                ? 'rounded border border-error px-4 py-2 text-label-md text-error'
                : 'rounded bg-primary px-4 py-2 text-label-md text-on-primary'
            }
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
