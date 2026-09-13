import { QueryClient } from '@tanstack/react-query'
import { isApiError } from './api'

/** Retry transport failures; never retry a stated condition.
 *
 * A 4xx is the server answering, not failing: `?account=cash` without live
 * credentials is a 409 with a reason, an out-of-range ceiling is a 422 with
 * the range. Retrying either produces the same answer three times and delays
 * the message the page is meant to render. A 5xx or an unreachable engine is
 * worth one more attempt.
 *
 * The 409 in particular must reach the UI quickly and intact — an
 * honest-looking empty ledger is exactly what it exists to prevent. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isApiError(error) && error.status >= 400 && error.status < 500) return false
  return failureCount < 1
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: shouldRetry,
    },
  },
})
