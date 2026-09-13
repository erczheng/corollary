import { isApiError, isUnreachable } from '../lib/api'

/** A request that genuinely failed, as opposed to a book that is not
 * configured.
 *
 * `error`, not `bearish`: this is a system condition. A losing position is
 * `bearish` and a failed fetch is `error`, and collapsing the two would make
 * a red page mean either "you lost money" or "the terminal is broken" with
 * no way to tell which.
 *
 * Unreachable gets its own sentence because every other failure means the
 * server said something and this one means it did not — "check the engine is
 * running" is actionable where "500" is not.
 *
 * Lives here rather than in a page because Activity and Account render the
 * identical thing, and the Dashboard and Markets will when they migrate. */
export function RequestFailed({ error, what }: { error: unknown; what: string }) {
  const message = isUnreachable(error)
    ? 'The engine did not answer. Check that it is running, then try again.'
    : isApiError(error)
      ? error.message
      : `Something went wrong loading ${what}.`

  return (
    <p role="alert" className="max-w-prose text-body-md text-error">
      {message}
    </p>
  )
}
