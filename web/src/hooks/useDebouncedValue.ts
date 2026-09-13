import { useEffect, useState } from 'react'

/** A value that settles before it is read.
 *
 * The search box on Activity is a *server* query now — every keystroke is a
 * request and a new cache key — so the text the box shows and the text the
 * query asks about are deliberately two different values. Typing stays
 * instant; the request waits for a pause.
 *
 * Not a throttle: a throttle would fire mid-word and put a page of results
 * for "AA" on screen under the word "AAPL". */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delayMs)
    return () => clearTimeout(timer)
  }, [value, delayMs])

  return settled
}
