import { useUIStore } from '../lib/store'

/** Manual / Auto switch — whether the engine may place orders unattended. */
export function ExecutionModeToggle() {
  const executionMode = useUIStore((s) => s.executionMode)
  const setExecutionMode = useUIStore((s) => s.setExecutionMode)

  return (
    <div className="flex rounded-full border border-outline bg-surface-container-low p-0.5 text-label-md">
      <button
        type="button"
        onClick={() => setExecutionMode('manual')}
        aria-pressed={executionMode === 'manual'}
        className={
          executionMode === 'manual'
            ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
            : 'rounded-full px-3 py-1 text-on-surface-variant'
        }
      >
        Manual
      </button>
      <button
        type="button"
        onClick={() => setExecutionMode('auto')}
        aria-pressed={executionMode === 'auto'}
        className={
          executionMode === 'auto'
            ? 'rounded-full bg-primary px-3 py-1 text-on-primary'
            : 'rounded-full px-3 py-1 text-on-surface-variant'
        }
      >
        Auto
      </button>
    </div>
  )
}
